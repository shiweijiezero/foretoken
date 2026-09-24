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

use foretoken_artifacts::ModelSource;
use foretoken_model_protocol::{
    CumulativeHistogram, RuntimeMetadataResponse, RuntimeModelIdentity, TelemetryResponse,
};
use foretoken_model_server::api::{AdmissionPermit, RuntimeHealth};
use foretoken_model_server::launch::{Lifecycle, append_engine_arg};
use foretoken_model_server::managed_engine::ManagedEngine;

const LAUNCH_PLAN_ENV: &str = "FORETOKEN_OMNI_LAUNCH_PLAN";
const LISTEN_ENV: &str = "FORETOKEN_INTERNAL_LISTEN";
const EXECUTABLE_ENV: &str = "FORETOKEN_OMNI_EXECUTABLE";

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct LaunchPlan {
    version: u8,
    model: String,
    source: ModelSource,
    revision: String,
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

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
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
    let child = ManagedEngine::spawn(engine_command(&plan)?, None).await?;
    if let Err(error) = wait_until_ready(
        &client,
        &upstream,
        &child,
        Duration::from_secs(plan.lifecycle.startup_seconds),
    )
    .await
    {
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
        model: plan.model.clone(),
        revision: plan.revision.clone(),
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
    shutdown.notify_waiters();
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
        ec_transfer: None,
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
        version: 2,
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
    let Some(guard) = state.health.try_admit() else {
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

fn engine_command(plan: &LaunchPlan) -> Result<Command, Box<dyn std::error::Error>> {
    let executable = std::env::var(EXECUTABLE_ENV).unwrap_or_else(|_| "vllm".into());
    let model = match plan.source {
        ModelSource::Local => local_artifact_path(&plan.model)?,
        ModelSource::Hf | ModelSource::ModelScope => plan.model.clone(),
    };
    let mut command = Command::new(executable);
    command
        .arg("serve")
        .arg(model)
        .arg("--omni")
        .arg("--host=127.0.0.1")
        .arg(format!("--port={}", plan.upstream_port))
        .args(render_engine_args(&plan.engine_args)?);
    match plan.source {
        ModelSource::Local => {}
        ModelSource::Hf | ModelSource::ModelScope => {
            command.arg(format!("--revision={}", plan.revision));
        }
    }
    if matches!(plan.source, ModelSource::ModelScope) {
        command.env("VLLM_USE_MODELSCOPE", "true");
    }
    Ok(command)
}

fn local_artifact_path(identifier: &str) -> std::io::Result<String> {
    let root = foretoken_artifacts::model_root();
    let path =
        foretoken_artifacts::resolve_directory(root.as_deref(), identifier)?.ok_or_else(|| {
            std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("local artifact {identifier:?} was not found"),
            )
        })?;
    path.into_os_string().into_string().map_err(|_| {
        std::io::Error::new(
            std::io::ErrorKind::InvalidData,
            "local artifact path is not UTF-8",
        )
    })
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

async fn wait_until_ready(
    client: &reqwest::Client,
    upstream: &str,
    child: &ManagedEngine,
    timeout: Duration,
) -> Result<(), Box<dyn std::error::Error>> {
    let deadline = Instant::now() + timeout;
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

async fn drain(health: &RuntimeHealth, timeout: Duration) {
    let deadline = Instant::now() + timeout;
    while health.running_requests() != 0 && Instant::now() < deadline {
        tokio::time::sleep(Duration::from_millis(250)).await;
    }
}

fn validate_plan(plan: &LaunchPlan) -> Result<(), String> {
    if plan.version != 1 {
        return Err(format!(
            "unsupported vLLM-Omni launch plan version {}",
            plan.version
        ));
    }
    if plan.model.is_empty() || plan.revision.is_empty() {
        return Err("model and revision must be nonempty".into());
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

#[cfg(unix)]
async fn shutdown_signal() -> Result<(), std::io::Error> {
    use tokio::signal::unix::{SignalKind, signal};
    let mut terminate = signal(SignalKind::terminate())?;
    tokio::select! {
        result = tokio::signal::ctrl_c() => result,
        _ = terminate.recv() => Ok(()),
    }
}

#[cfg(not(unix))]
async fn shutdown_signal() -> Result<(), std::io::Error> {
    tokio::signal::ctrl_c().await
}
