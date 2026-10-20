// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Supervises a private Python DT role behind the shared Rust admission owner.

use std::future::IntoFuture;
use std::io;
use std::process::Command;
use std::sync::Arc;
use std::time::{Duration, Instant};

use foretoken_model_server::backend::vllm::startup;
use foretoken_model_server::{
    api::RuntimeHealth, backend::vllm::config::RuntimeConfig, dt_ingress, engine::ManagedEngine,
    instance_admission::InstanceAdmissionWatcher, runtime_cache,
};
use foretoken_request_ledger::RequestLedger;
use tokio::net::TcpListener;
use tokio::sync::Notify;
use tokio_util::sync::CancellationToken;
use tracing::warn;
use vllm_managed_engine::allocate_handshake_port;

/// Starts one role with shared admission and drains its sessions before stopping the process group.
pub(super) async fn run(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&foretoken_model_server::profiling::Config>,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
    cache_shutdown: CancellationToken,
    ledger: Option<Arc<RequestLedger>>,
) -> Result<(), Box<dyn std::error::Error>> {
    let health = Arc::new(RuntimeHealth::new());
    health.set_startup_admission_limit(config.startup_admission_limit);
    let watcher = config
        .instance_admission_path
        .clone()
        .map(|path| InstanceAdmissionWatcher::load(path, health.clone()))
        .transpose()?;
    // Port ownership must precede backend epoch recovery, as on the ordinary engine path.
    let listener = TcpListener::bind(config.listen_address).await?;
    let execution_store = if let Some(ledger) = ledger {
        let pod_uid = super::required_env("FORETOKEN_POD_UID")?;
        let epoch = ledger.register_backend(&pod_uid).await?;
        Some((ledger, pod_uid, epoch))
    } else {
        None
    };
    let port = allocate_handshake_port("127.0.0.1")?;
    let endpoint = format!("http://127.0.0.1:{port}");
    let (engine, mode, _) = startup::start_with_cache_fallback(
        config,
        Instant::now() + config.launch.startup_timeout(),
        async |mode, deadline| {
            start_role(
                config,
                cache,
                profiling,
                cache_server,
                mode,
                deadline,
                (port, &endpoint),
            )
            .await
        },
    )
    .await?;
    let profile_client = reqwest::Client::builder().no_proxy().build()?;
    let mut profiler = profiling
        .filter(|_| mode == runtime_cache::Mode::Persistent)
        .map(|config| {
            foretoken_model_server::profiling::Supervisor::new(
                config.clone(),
                Some(
                    foretoken_model_server::profiling::CaptureBackend::DraftTarget {
                        endpoint: endpoint.clone(),
                        client: profile_client,
                    },
                ),
            )
        });
    let app = dt_ingress::router(
        endpoint.clone(),
        health.clone(),
        execution_store,
        config.launch.internal_generate_request_body_limit_bytes,
        profiler
            .as_ref()
            .map(foretoken_model_server::profiling::Supervisor::handle),
    )?;
    health.set_process_alive(true);
    health.set_client_healthy(true);
    health.set_accepting(true);
    let shutdown = Arc::new(Notify::new());
    let server_shutdown = shutdown.clone();
    let mut server = Box::pin(
        axum::serve(listener, app)
            .with_graceful_shutdown(async move { server_shutdown.notified().await })
            .into_future(),
    );
    let mut server_finished = false;
    let failure = tokio::select! {
        biased;
        never = async {
            match watcher { Some(watcher) => watcher.run().await,
                None => std::future::pending::<std::convert::Infallible>().await }
        } => match never {},
        result = foretoken_model_server::shutdown_signal() => result.err().map(|error| error.to_string()),
        () = health.execution_failed() => Some("DT request termination could not be confirmed".into()),
        result = async {
            match &mut profiler {
                Some(profiler) => profiler.run().await,
                None => std::future::pending().await,
            }
        } => Some(result.err().unwrap_or_else(|| "capture supervisor stopped".into())),
        status = engine.wait_for_exit() => Some(format!("DT role exited: {status}")),
        reason = runtime_cache::wait_for_server(cache_server) => Some(reason),
        error = startup::wait_cache_write_failure(cache, mode) => Some(error.to_string()),
        result = &mut server => { server_finished = true; Some(format!("DT ingress stopped: {result:?}")) },
    };
    health.set_accepting(false);
    let deadline = tokio::time::Instant::now() + config.launch.drain_timeout();
    let active_capture = profiler
        .as_ref()
        .is_some_and(|profiler| profiler.has_native_capture());
    if failure.is_none() && !active_capture {
        // Keep the listener polling while admitted streams, verification and DMA ACKs drain.
        let drain = async {
            drain(&endpoint).await?;
            health.wait_drained().await;
            Ok::<_, reqwest::Error>(())
        };
        tokio::select! {
            result = tokio::time::timeout_at(deadline, drain) => match result {
                Ok(Ok(())) => {},
                Ok(Err(error)) => warn!(%error, "DT admission drain failed"),
                Err(_) => warn!("DT sessions did not drain before deadline"),
            },
            _ = &mut server => { server_finished = true; },
            _ = engine.wait_for_exit() => {},
        }
    }
    cache_shutdown.cancel();
    let remaining = deadline.saturating_duration_since(tokio::time::Instant::now());
    if active_capture {
        engine.shutdown(remaining).await?;
        engine.wait_for_exit().await;
        profiler
            .as_mut()
            .expect("active profiler")
            .engine_stopped("diagnostic runtime terminated")
            .await;
    } else {
        engine.shutdown_application(remaining).await?;
    }
    health.set_process_alive(false);
    health.set_client_healthy(false);
    shutdown.notify_waiters();
    // Request tasks can now persist termination evidence without retaining a live engine slot.
    if !server_finished {
        let _ = tokio::time::timeout_at(deadline, &mut server).await;
    }
    match failure {
        Some(reason) => Err(io::Error::other(reason).into()),
        None => Ok(()),
    }
}

// Startup uses the same cache/artifact preparation and deadline as the native EngineCore path.
async fn start_role(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&foretoken_model_server::profiling::Config>,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
    mode: runtime_cache::Mode,
    deadline: Instant,
    address: (u16, &str),
) -> Result<ManagedEngine, startup::EngineStartupFailure> {
    let (port, endpoint) = address;
    let prepared = startup::prepare_engine_launch(config, cache, profiling, mode, deadline).await?;
    let artifacts = &config.launch.artifacts;
    let draft = config
        .launch
        .dt
        .as_ref()
        .and_then(|dt| dt.draft_artifacts.as_ref());
    let mut command = Command::new(foretoken_model_server::engine::python_executable());
    command.envs(prepared.environment)
        .env("VLLM_USE_V2_MODEL_RUNNER", "1")
        .env("FORETOKEN_DT_MODEL_METADATA", serde_json::json!({
            "model": artifacts.model,
            "revision": (artifacts.source != foretoken_artifacts::ModelSource::Local).then_some(&artifacts.revision),
            "tokenizer": artifacts.tokenizer,
            "tokenizer_revision": (artifacts.source != foretoken_artifacts::ModelSource::Local).then_some(&artifacts.tokenizer_revision),
            "prepared_tokenizer": prepared.prepared_tokenizer,
            "resolved_revision": prepared.resolved_revision,
            "resolved_draft_revision": prepared.resolved_draft_revision,
            "draft_model": draft.map_or(&artifacts.model, |draft| &draft.model),
        }).to_string())
        .args(["-m", "foretoken_dt.serve", "--role", config.launch.dt.as_ref().expect("DT launch selected").role.as_str()])
        .arg(format!("--model={}", prepared.model))
        .args(["--host=127.0.0.1".to_owned(), format!("--port={port}")])
        .args(prepared.arguments);
    if let Some(address) = config.dt_rdma_address {
        command.arg(format!("--rdma-host={address}"));
    }
    let client = reqwest::Client::builder()
        .no_proxy()
        .build()
        .map_err(|error| startup::EngineStartupFailure::Other(io::Error::other(error)))?;
    let engine = ManagedEngine::spawn(
        command,
        profiling.filter(|_| mode == runtime_cache::Mode::Persistent),
    )
    .await
    .map_err(|error| startup::EngineStartupFailure::Other(io::Error::other(error)))?;
    let readiness = async {
        loop {
            if client
                .get(format!("{endpoint}/status?observations=false"))
                .send()
                .await
                .is_ok_and(|response| response.status().is_success())
            {
                return;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    };
    let failure = tokio::select! {
        () = readiness => return Ok(engine),
        () = tokio::time::sleep_until(deadline.into()) => startup::classify_engine_startup_failure(cache, mode, "DT role startup deadline elapsed".into()),
        status = engine.wait_for_exit() => startup::classify_engine_startup_failure(cache, mode, format!("DT role exited during startup: {status}")),
        reason = runtime_cache::wait_for_server(cache_server) => startup::EngineStartupFailure::Other(io::Error::other(reason)),
        error = startup::wait_cache_write_failure(cache, mode) => startup::cache_mode_failure(mode, "DT RuntimeCache write failed", error),
    };
    engine
        .shutdown_application(config.launch.drain_timeout())
        .await
        .map_err(|error| startup::EngineStartupFailure::Other(io::Error::other(error)))?;
    Err(failure)
}

// The role owns sessions and transfer storage; shutdown waits for both before terminating workers.
async fn drain(endpoint: &str) -> Result<(), reqwest::Error> {
    let client = reqwest::Client::builder().no_proxy().build()?;
    client
        .post(format!("{endpoint}/v1/internal/admission/close"))
        .send()
        .await?
        .error_for_status()?;
    #[derive(serde::Deserialize)]
    struct RoleDrainStatus {
        active_sessions: usize,
        retained_artifacts: usize,
    }
    loop {
        let status: RoleDrainStatus = client
            .get(format!("{endpoint}/status"))
            .send()
            .await?
            .error_for_status()?
            .json()
            .await?;
        if status.active_sessions == 0 && status.retained_artifacts == 0 {
            return Ok(());
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
    }
}
