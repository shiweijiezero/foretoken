// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Starts a managed local EngineCore child and serves the restricted internal API.

use std::future::IntoFuture;
use std::sync::Arc;
use std::time::Instant;

use foretoken_model_protocol::{ModelDtype, RuntimeMetadataResponse, RuntimeModelIdentity};
use foretoken_model_server::core::api::{AppState, RuntimeHealth, router};
use foretoken_model_server::core::config::RuntimeConfig;
use foretoken_model_server::core::kv_events::KvEventAdapter;
use foretoken_model_server::engine::sglang::{SglangBackend, SglangLaunchPlan, SglangProcess};
use foretoken_model_server::engine::vllm::{
    LaunchPlanV1, VllmBackend, VllmProcess, conversion::to_neutral_model_dtype,
};
use foretoken_model_server::engine::{Engine, EngineKind};
use tokio::net::TcpListener;
use tokio::sync::Notify;
use tracing::{error, info, warn};
use vllm_llm::Llm;

const KV_KEY_PATH_ENV: &str = "FORETOKEN_KV_INDEX_KEY_PATH";
const KV_SCOPE_ENV: &str = "FORETOKEN_KV_SCOPE_ID";
const MODEL_GROUP_UID_ENV: &str = "FORETOKEN_MODEL_GROUP_UID";

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    vllm_tracing::init_tracing("ForetokenModelServer");

    // Resolve the controller-owned launch plan before starting any engine or network task.
    let config = RuntimeConfig::from_env().map_err(std::io::Error::other)?;

    match config.launch.kind {
        EngineKind::Vllm => run_vllm(config).await,
        EngineKind::Sglang => run_sglang(config).await,
    }
}

async fn run_vllm(config: RuntimeConfig) -> Result<(), Box<dyn std::error::Error>> {
    let plan = LaunchPlanV1::parse(&config.launch.payload).map_err(std::io::Error::other)?;

    // Resolve optional KV projection state now; connect only after the engine publisher is ready.
    let kv_events = match kv_event_adapter(&plan) {
        Ok(adapter) => Some(adapter),
        Err(error) => {
            warn!(%error, "KV index configuration is unavailable; prefix scoring is disabled");
            None
        }
    };

    let mut process = VllmProcess::spawn(&plan).await?;
    let health = Arc::new(RuntimeHealth::new());
    health.set_process_alive(true);

    let mut client_health = process.client().subscribe_health();
    let max_concurrent_requests = process.max_concurrent_requests()?;
    let metadata = RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: plan.artifacts.model.clone(),
            revision: plan.artifacts.revision.clone(),
        },
        model_dtype: to_neutral_model_dtype(process.client().model_dtype()),
        effective_max_model_len: process.client().max_model_len(),
        ec_transfer: plan.ec.runtime_metadata(),
        capabilities: if plan.ec.enabled() {
            ["ec_transfer".into()].into_iter().collect()
        } else {
            Default::default()
        },
    };
    if !*client_health.borrow() {
        let reason = process.client().health_error().map_or_else(
            || "EngineCore client became unhealthy during startup".to_string(),
            |error| format!("EngineCore client became unhealthy during startup: {error}"),
        );
        let _ = process.take_client().shutdown().await;
        let _ = process.shutdown(plan.drain_timeout()).await;
        health.set_process_alive(false);
        return Err(std::io::Error::other(reason).into());
    }
    let kv_events = if let Some(adapter) = kv_events {
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(adapter.clone().serve(ready_tx));
        if ready_rx.await == Ok(true) {
            Some(adapter)
        } else {
            warn!("KV event subscriber is unavailable; prefix scoring is disabled");
            None
        }
    } else {
        None
    };
    health.set_client_healthy(true);
    health.set_accepting(true);
    let backend = Arc::new(VllmBackend::new(
        Llm::new(process.take_client()),
        max_concurrent_requests,
    ));

    // Expose only the restricted group-local API after EngineCore is connected and healthy.
    let listener = match TcpListener::bind(config.listen_address).await {
        Ok(listener) => listener,
        Err(error) => {
            health.set_accepting(false);
            health.set_client_healthy(false);
            let _ = backend.cleanup().await;
            let _ = process.shutdown(plan.drain_timeout()).await;
            return Err(error.into());
        }
    };
    let shutdown = Arc::new(Notify::new());
    let server_shutdown = shutdown.clone();
    let mut app_state = AppState::new(backend.clone(), health.clone(), metadata);
    if let Some(kv_events) = kv_events {
        app_state = app_state.with_kv_events(kv_events);
    }
    let mut server = Box::pin(
        axum::serve(
            listener,
            router(app_state, plan.internal_generate_request_body_limit_bytes),
        )
        .with_graceful_shutdown(async move { server_shutdown.notified().await })
        .into_future(),
    );

    // Signals, client health, child exit, and server failure converge on one shutdown path.
    enum Stop {
        Signal,
        ClientUnhealthy(String),
        ChildExited(String),
        Server(String),
    }
    let stop = tokio::select! {
        () = shutdown_signal() => Stop::Signal,
        changed = client_health.changed() => {
            if changed.is_err() || !*client_health.borrow() {
                Stop::ClientUnhealthy("EngineCore client became unhealthy".into())
            } else {
                unreachable!("EngineCore health only transitions to unhealthy")
            }
        },
        status = process.engine.wait_for_exit() => Stop::ChildExited(format!("managed EngineCore exited unexpectedly: {status}")),
        result = &mut server => Stop::Server(match result {
            Ok(()) => "HTTP server stopped unexpectedly".into(),
            Err(error) => format!("HTTP server failed: {error}"),
        }),
    };
    match &stop {
        Stop::Signal => info!("received shutdown signal"),
        Stop::ClientUnhealthy(reason) | Stop::ChildExited(reason) | Stop::Server(reason) => {
            warn!(%reason)
        }
    }

    // Stop new admission before draining HTTP handlers, the client, and finally the child process.
    health.set_accepting(false);
    health.set_client_healthy(false);
    shutdown.notify_waiters();
    let deadline = Instant::now() + plan.drain_timeout();
    if !matches!(&stop, Stop::Server(_)) {
        match tokio::time::timeout(plan.drain_timeout(), server.as_mut()).await {
            Ok(Ok(())) => {}
            Ok(Err(error)) => error!(%error, "HTTP server failed while draining"),
            Err(_) => {
                warn!("HTTP handlers did not drain before deadline");
                drop(server);
            }
        }
    }
    if let Err(error) = backend.cleanup().await {
        warn!(%error, "could not shut down EngineCore client cleanly");
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    if let Err(error) = process.shutdown(remaining).await {
        warn!(%error, "could not shut down managed EngineCore cleanly");
    }
    health.set_process_alive(false);

    match stop {
        Stop::Signal => Ok(()),
        Stop::ClientUnhealthy(reason) | Stop::ChildExited(reason) | Stop::Server(reason) => {
            Err(std::io::Error::other(reason).into())
        }
    }
}

async fn run_sglang(config: RuntimeConfig) -> Result<(), Box<dyn std::error::Error>> {
    let plan = SglangLaunchPlan::parse(&config.launch.payload).map_err(std::io::Error::other)?;

    let health = Arc::new(RuntimeHealth::new());
    health.set_process_alive(true);

    let mut process = SglangProcess::spawn(&plan)?;

    // Wait for the SGLang HTTP server to become healthy within the startup budget.
    if let Err(reason) = wait_for_sglang_health(&plan, plan.startup_seconds).await {
        health.set_process_alive(false);
        let _ = process.shutdown(plan.drain_timeout()).await;
        return Err(std::io::Error::other(reason).into());
    }

    let metadata = RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: plan.model.clone(),
            revision: plan.revision.clone().unwrap_or_default(),
        },
        model_dtype: ModelDtype::BFloat16,
        effective_max_model_len: 0,
        ec_transfer: None,
        capabilities: Default::default(),
    };

    health.set_client_healthy(true);
    health.set_accepting(true);
    let backend = Arc::new(SglangBackend::new(format!(
        "http://127.0.0.1:{}",
        plan.port
    )));

    let listener = match TcpListener::bind(config.listen_address).await {
        Ok(listener) => listener,
        Err(error) => {
            health.set_accepting(false);
            health.set_client_healthy(false);
            let _ = backend.cleanup().await;
            let _ = process.shutdown(plan.drain_timeout()).await;
            return Err(error.into());
        }
    };
    let shutdown = Arc::new(Notify::new());
    let server_shutdown = shutdown.clone();
    let app_state = AppState::new(backend.clone(), health.clone(), metadata);
    let mut server = Box::pin(
        axum::serve(
            listener,
            router(app_state, plan.internal_generate_request_body_limit_bytes),
        )
        .with_graceful_shutdown(async move { server_shutdown.notified().await })
        .into_future(),
    );

    enum Stop {
        Signal,
        ChildExited(String),
        Server(String),
    }
    let stop = tokio::select! {
        () = shutdown_signal() => Stop::Signal,
        status = process.wait_for_exit() => Stop::ChildExited(match status {
            Ok(status) => format!("sglang server exited unexpectedly: {status}"),
            Err(error) => format!("sglang server wait failed: {error}"),
        }),
        result = &mut server => Stop::Server(match result {
            Ok(()) => "HTTP server stopped unexpectedly".into(),
            Err(error) => format!("HTTP server failed: {error}"),
        }),
    };
    match &stop {
        Stop::Signal => info!("received shutdown signal"),
        Stop::ChildExited(reason) | Stop::Server(reason) => warn!(%reason),
    }

    health.set_accepting(false);
    health.set_client_healthy(false);
    shutdown.notify_waiters();
    let deadline = Instant::now() + plan.drain_timeout();
    if !matches!(&stop, Stop::Server(_)) {
        match tokio::time::timeout(plan.drain_timeout(), server.as_mut()).await {
            Ok(Ok(())) => {}
            Ok(Err(error)) => error!(%error, "HTTP server failed while draining"),
            Err(_) => {
                warn!("HTTP handlers did not drain before deadline");
                drop(server);
            }
        }
    }
    if let Err(error) = backend.cleanup().await {
        warn!(%error, "could not shut down SGLang backend cleanly");
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    if let Err(error) = process.shutdown(remaining).await {
        warn!(%error, "could not shut down SGLang server cleanly");
    }
    health.set_process_alive(false);

    match stop {
        Stop::Signal => Ok(()),
        Stop::ChildExited(reason) | Stop::Server(reason) => {
            Err(std::io::Error::other(reason).into())
        }
    }
}

/// Polls the SGLang `/health` endpoint until it reports healthy or the budget
/// expires.
async fn wait_for_sglang_health(
    plan: &SglangLaunchPlan,
    budget_seconds: u64,
) -> Result<(), String> {
    let client = reqwest::Client::new();
    let url = format!("http://127.0.0.1:{}/health", plan.port);
    let deadline = tokio::time::Instant::now() + std::time::Duration::from_secs(budget_seconds);
    loop {
        match client.get(&url).send().await {
            Ok(response) if response.status().is_success() => return Ok(()),
            Ok(_) => {}
            Err(_) => {}
        }
        if tokio::time::Instant::now() >= deadline {
            return Err("sglang server did not become healthy within the startup budget".into());
        }
        tokio::time::sleep(std::time::Duration::from_millis(250)).await;
    }
}

fn kv_event_adapter(
    plan: &LaunchPlanV1,
) -> Result<Arc<KvEventAdapter>, Box<dyn std::error::Error>> {
    let bytes = std::fs::read(std::env::var(KV_KEY_PATH_ENV)?)?;
    let key: [u8; 32] = bytes
        .as_slice()
        .try_into()
        .map_err(|_| "KV index key must be exactly 32 bytes")?;
    let scope_id = required_env(KV_SCOPE_ENV)?;
    let model_group_id = required_env(MODEL_GROUP_UID_ENV)?;
    Ok(KvEventAdapter::new(
        key,
        scope_id,
        model_group_id,
        plan.artifacts.revision.clone(),
        plan.parallelism.dp.try_into()?,
    ))
}

fn required_env(name: &str) -> Result<String, Box<dyn std::error::Error>> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{name} must be set by the ModelGroup controller").into())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("installing Ctrl-C signal handler must succeed");
    };
    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("installing SIGTERM signal handler must succeed")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! { () = ctrl_c => {}, () = terminate => {} }
}
