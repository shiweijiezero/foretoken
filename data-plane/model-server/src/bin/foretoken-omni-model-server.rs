// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Thin lifecycle and HTTP boundary for a managed vLLM-Omni server.

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::process::Command;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderMap, HeaderName, Method, Request, Response, StatusCode};
use axum::response::IntoResponse;
use axum::routing::{get, post};
use axum::{Json, Router};
use futures::StreamExt;
use serde::Deserialize;
use serde_json::Value;
use tokio::net::TcpListener;
use tokio::sync::Notify;

use foretoken_artifacts::{ModelArtifacts, ModelSource};
use foretoken_model_protocol::{
    CumulativeHistogram, RuntimeMetadataResponse, RuntimeModelIdentity, TelemetryResponse,
};
use foretoken_model_server::api::{AdmissionPermit, RuntimeHealth};
use foretoken_model_server::engine::{
    Lifecycle, ManagedEngine, append_engine_arg, python_executable,
};
use foretoken_model_server::model_cache::preparation::{self, PreparationStorage};
use foretoken_model_server::shutdown_signal;

const LAUNCH_PLAN_ENV: &str = "FORETOKEN_OMNI_LAUNCH_PLAN";
const LISTEN_ENV: &str = "FORETOKEN_INTERNAL_LISTEN";
const EXECUTABLE_ENV: &str = "FORETOKEN_OMNI_EXECUTABLE";

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct LaunchPlan {
    version: u8,
    artifacts: ModelArtifacts,
    upstream_port: u16,
    lifecycle: Lifecycle,
    #[serde(default)]
    engine_args: BTreeMap<String, Value>,
}

#[derive(Clone)]
struct AppState {
    client: reqwest::Client,
    upstream: String,
    model: String,
    revision: String,
    health: Arc<RuntimeHealth>,
}

/// Dispatch model preparation or serve video requests while owning the native engine lifetime.
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let python = python_executable();
    if let Some(action @ ("prepare" | "resolve")) = std::env::args().nth(1).as_deref() {
        let artifacts = ModelArtifacts::from_env()?;
        if action == "prepare" {
            preparation::prepare_job(&artifacts, &python).await?;
        } else {
            preparation::resolve(&artifacts, &python).await?;
        }
        return Ok(());
    }

    let plan: LaunchPlan = serde_json::from_str(&required_env(LAUNCH_PLAN_ENV)?)?;
    validate_plan(&plan)?;
    let listen: SocketAddr = required_env(LISTEN_ENV)?.parse()?;
    if listen.port() == plan.upstream_port {
        return Err("adapter and vLLM-Omni ports must differ".into());
    }

    let upstream = format!("http://127.0.0.1:{}", plan.upstream_port);
    let client = reqwest::Client::builder()
        .connect_timeout(Duration::from_secs(5))
        .build()?;
    let listener = TcpListener::bind(listen).await?;
    let deadline = Instant::now() + Duration::from_secs(plan.lifecycle.startup_seconds);
    // Remote sources consume the fixed publication through the shared preparation lifecycle.
    let model = if plan.artifacts.source == ModelSource::Local {
        preparation::local_artifact_path(&plan.artifacts.model)?
    } else {
        let model_root = foretoken_artifacts::model_cache_root()
            .ok_or("FORETOKEN_MODEL_CACHE_ROOT must be set for model preparation")?;
        let state_root = foretoken_artifacts::preparation_root()
            .ok_or("FORETOKEN_MODEL_PREPARATION_ROOT must be set for model preparation")?;
        let storage = PreparationStorage {
            model_root: &model_root,
            state_root: &state_root,
            reusable_roots: None,
            environment: &[],
        };
        let scope = required_env(preparation::PREPARATION_SCOPE_ENV)?;
        tokio::time::timeout_at(
            deadline.into(),
            preparation::prepare(&plan.artifacts, &python, &storage, &scope, true),
        )
        .await
        .map_err(|_| "model preparation exceeded the startup deadline")??
        .model
        .display()
        .to_string()
    };
    let child = ManagedEngine::spawn(engine_command(&plan, &model)?, None).await?;
    if let Err(error) = wait_until_ready(&client, &upstream, &child, deadline).await {
        child.shutdown(Duration::from_secs(10)).await?;
        return Err(error);
    }

    let health = Arc::new(RuntimeHealth::new());
    health.set_process_alive(true);
    health.set_client_healthy(true);
    health.set_accepting(true);
    let state = AppState {
        client,
        upstream,
        model: plan.artifacts.model.clone(),
        revision: plan.artifacts.revision.clone(),
        health: health.clone(),
    };
    let app = router(state);
    eprintln!("Foretoken vLLM-Omni adapter ready on {listen}");

    let shutdown = Arc::new(Notify::new());
    let server_shutdown = shutdown.clone();
    let mut server = tokio::spawn(async move {
        axum::serve(listener, app)
            .with_graceful_shutdown(async move { server_shutdown.notified().await })
            .await
    });
    enum Stop {
        Signal,
        Child(std::process::ExitStatus),
        Server,
        Error(Box<dyn std::error::Error>),
    }
    let stop = tokio::select! {
        signal = shutdown_signal() => match signal {
            Ok(()) => Stop::Signal,
            Err(error) => Stop::Error(Box::new(error)),
        },
        status = child.wait_for_exit() => Stop::Child(status),
        result = &mut server => match result {
            Ok(Ok(())) => Stop::Server,
            Ok(Err(error)) => Stop::Error(Box::new(error)),
            Err(error) => Stop::Error(Box::new(error)),
        },
    };
    let graceful = matches!(&stop, Stop::Signal);
    let stop_reason = match &stop {
        Stop::Signal => "shutdown signal".to_string(),
        Stop::Child(status) => format!("vLLM-Omni exited: {status}"),
        Stop::Server => "HTTP server stopped".to_string(),
        Stop::Error(error) => format!("adapter error: {error}"),
    };
    eprintln!("Stopping Foretoken vLLM-Omni adapter: {stop_reason}");
    health.set_accepting(false);
    if graceful {
        drain(&health, Duration::from_secs(plan.lifecycle.drain_seconds)).await;
    }
    health.set_process_alive(false);
    // Retain shutdown for the single HTTP task even if it has not polled its signal yet.
    shutdown.notify_one();
    if !server.is_finished()
        && tokio::time::timeout(Duration::from_secs(5), &mut server)
            .await
            .is_err()
    {
        server.abort();
    }
    child.shutdown(Duration::from_secs(10)).await?;
    if let Stop::Error(error) = stop {
        return Err(error);
    }
    Ok(())
}

/// Build video and control routes sharing the adapter's execution-admission state.
fn router(state: AppState) -> Router {
    Router::new()
        .route("/health", get(healthz))
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/metrics", get(metrics))
        .route("/v1/internal/metadata", get(metadata))
        .route("/v1/internal/telemetry", get(telemetry))
        .route("/v1/internal/admission/close", post(close_admission))
        .route("/v1/videos/sync", post(proxy_video))
        .with_state(state)
}

async fn healthz(State(state): State<AppState>) -> StatusCode {
    status(runtime_healthy(&state).await)
}

async fn readyz(State(state): State<AppState>) -> StatusCode {
    status(runtime_healthy(&state).await)
}

async fn runtime_healthy(state: &AppState) -> bool {
    state.health.ready()
        && matches!(
            state
                .client
                .get(format!("{}/health", state.upstream))
                .timeout(Duration::from_secs(1))
                .send()
                .await,
            Ok(response) if response.status().is_success()
        )
}

async fn metadata(State(state): State<AppState>) -> Json<RuntimeMetadataResponse> {
    Json(RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: state.model,
            revision: state.revision,
        },
        model_dtype: None,
        effective_max_model_len: 0,
        max_logprobs: None,
        ec_transfer: None,
        prepared_tokenizer: None,
        capabilities: ["video".to_owned()].into_iter().collect(),
    })
}

async fn telemetry(State(state): State<AppState>) -> Json<TelemetryResponse> {
    Json(telemetry_response(&state.health))
}

async fn close_admission(State(state): State<AppState>) -> Json<TelemetryResponse> {
    state.health.set_accepting(false);
    Json(telemetry_response(&state.health))
}

fn telemetry_response(health: &RuntimeHealth) -> TelemetryResponse {
    TelemetryResponse {
        version: 3,
        request_cost: Default::default(),
        collected_at_unix_ms: std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .min(u128::from(u64::MAX)) as u64,
        accepting: health.accepting(),
        data_parallel_ranks: Vec::new(),
        running_requests: health.running_requests(),
        max_concurrent_requests: None,
        scheduler_running_requests: None,
        scheduler_waiting_requests: None,
        kv_cache_usage: None,
        prompt_tokens_total: None,
        generation_tokens_total: None,
        ttft_seconds: CumulativeHistogram::default(),
        tpot_seconds: CumulativeHistogram::default(),
        e2e_seconds: CumulativeHistogram::default(),
    }
}

async fn metrics(State(state): State<AppState>) -> Response<Body> {
    proxy_request(
        &state,
        Method::GET,
        "/metrics",
        HeaderMap::new(),
        Body::empty(),
        None,
    )
    .await
}

async fn proxy_video(State(state): State<AppState>, request: Request<Body>) -> Response<Body> {
    if !state.health.ready() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let Ok(guard) = state.health.try_admit() else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    let (parts, body) = request.into_parts();
    proxy_request(
        &state,
        Method::POST,
        "/v1/videos/sync",
        parts.headers,
        body,
        Some(guard),
    )
    .await
}

/// Proxy one upstream request, retaining its admission permit until the response stream ends.
async fn proxy_request(
    state: &AppState,
    method: Method,
    path: &str,
    headers: HeaderMap,
    body: Body,
    guard: Option<AdmissionPermit>,
) -> Response<Body> {
    let has_body = method != Method::GET && method != Method::HEAD;
    let mut request = state
        .client
        .request(method, format!("{}{path}", state.upstream));
    for (name, value) in &headers {
        if !hop_by_hop(name) && name != axum::http::header::HOST {
            request = request.header(name, value);
        }
    }
    if has_body {
        request = request.body(reqwest::Body::wrap_stream(body.into_data_stream()));
    }
    let upstream = match request.send().await {
        Ok(response) => response,
        Err(_) => return StatusCode::BAD_GATEWAY.into_response(),
    };
    let status = upstream.status();
    let headers = upstream.headers().clone();
    let mut bytes = Box::pin(upstream.bytes_stream());
    let stream = async_stream::stream! {
        let _guard = guard;
        while let Some(item) = bytes.next().await {
            yield item;
        }
    };
    let mut response = Response::builder().status(status);
    if let Some(response_headers) = response.headers_mut() {
        for (name, value) in &headers {
            if !hop_by_hop(name) {
                response_headers.append(name, value.clone());
            }
        }
    }
    response
        .body(Body::from_stream(stream))
        .unwrap_or_else(|_| StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

fn hop_by_hop(name: &HeaderName) -> bool {
    matches!(
        name.as_str(),
        "connection"
            | "keep-alive"
            | "proxy-authenticate"
            | "proxy-authorization"
            | "te"
            | "trailer"
            | "transfer-encoding"
            | "upgrade"
    )
}

/// Build the native Omni command from the model snapshot supplied by shared preparation.
fn engine_command(plan: &LaunchPlan, model: &str) -> Result<Command, Box<dyn std::error::Error>> {
    let executable = std::env::var(EXECUTABLE_ENV).unwrap_or_else(|_| "vllm".into());
    let mut command = Command::new(executable);
    command
        .arg("serve")
        .arg(model)
        .arg("--omni")
        .arg("--host=127.0.0.1")
        .arg(format!("--port={}", plan.upstream_port))
        .args(render_engine_args(&plan.engine_args)?);
    if !plan.engine_args.contains_key("served-model-name") {
        command.arg(format!("--served-model-name={}", plan.artifacts.model));
    }
    command.env("VLLM_USE_MODELSCOPE", "false");
    Ok(command)
}

fn render_engine_args(args: &BTreeMap<String, Value>) -> Result<Vec<String>, String> {
    let mut output = Vec::new();
    for (name, value) in args {
        if name.is_empty()
            || name.starts_with('-')
            || matches!(name.as_str(), "host" | "model" | "omni" | "port")
        {
            return Err(format!("engineArgs.{name} is reserved or invalid"));
        }
        append_engine_arg(&mut output, name, value)?;
    }
    Ok(output)
}

/// Observe native health and child exit until the startup budget is exhausted.
async fn wait_until_ready(
    client: &reqwest::Client,
    upstream: &str,
    child: &ManagedEngine,
    deadline: Instant,
) -> Result<(), Box<dyn std::error::Error>> {
    loop {
        if let Some(status) = child.try_wait().await {
            return Err(format!("vLLM-Omni exited during startup: {status}").into());
        }
        if matches!(
            client
                .get(format!("{upstream}/health"))
                .timeout(Duration::from_secs(1))
                .send()
                .await,
            Ok(response) if response.status().is_success()
        ) {
            return Ok(());
        }
        if Instant::now() >= deadline {
            return Err("vLLM-Omni startup deadline elapsed".into());
        }
        tokio::time::sleep(Duration::from_millis(500)).await;
    }
}

/// Wait for admitted video streams to finish within the serving shutdown budget.
async fn drain(health: &RuntimeHealth, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    while health.running_requests() != 0 && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

/// Validate the controller's Omni launch contract before allocating serving resources.
fn validate_plan(plan: &LaunchPlan) -> Result<(), String> {
    if plan.version != 2 {
        return Err(format!(
            "unsupported vLLM-Omni launch plan version {}",
            plan.version
        ));
    }
    for (name, value) in [
        ("model", &plan.artifacts.model),
        ("revision", &plan.artifacts.revision),
        ("tokenizer", &plan.artifacts.tokenizer),
        ("tokenizerRevision", &plan.artifacts.tokenizer_revision),
    ] {
        if value.is_empty() {
            return Err(format!("artifacts.{name} must be nonempty"));
        }
    }
    if plan.upstream_port == 0 {
        return Err("upstreamPort must be positive".into());
    }
    if plan.lifecycle.startup_seconds == 0 || plan.lifecycle.drain_seconds == 0 {
        return Err("lifecycle timeouts must be positive".into());
    }
    render_engine_args(&plan.engine_args).map(|_| ())
}

fn required_env(name: &str) -> Result<String, String> {
    match std::env::var(name) {
        Ok(value) if !value.is_empty() => Ok(value),
        _ => Err(format!("{name} must be set")),
    }
}

fn status(value: bool) -> StatusCode {
    if value {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}
