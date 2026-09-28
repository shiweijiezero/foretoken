// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Supervises the installed DT role application with the normal model-server process owner.

use std::io;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use foretoken_model_server::{
    config::RuntimeConfig, launch::PYTHON_MODULE_PATH, managed_engine::ManagedEngine, runtime_cache,
};
use tokio::sync::Notify;
use tracing::warn;

/// Starts one role and drains its sessions before terminating the managed engine process group.
pub(super) async fn run(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
    cache_shutdown: Arc<Notify>,
) -> Result<(), Box<dyn std::error::Error>> {
    let mode = runtime_cache::Mode::Persistent;
    let mut environment = if let Some(cache) = cache {
        cache.set_mode(mode);
        cache.prepare(mode)?;
        cache.engine_environment(mode)
    } else {
        Vec::new()
    };
    let model_root = cache
        .map(|cache| cache.model_root(mode))
        .or_else(foretoken_artifacts::model_root)
        .unwrap_or_else(|| PathBuf::from(super::TEMPORARY_MODEL_SOURCE_ROOT));
    environment.extend(config.launch.source_environment(&model_root));
    let mut paths = vec![PathBuf::from(PYTHON_MODULE_PATH)];
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        paths.extend(std::env::split_paths(&existing));
    }
    let mut command = Command::new(config.launch.python_executable());
    command
        .envs(environment)
        .env("PYTHONPATH", std::env::join_paths(paths)?)
        .env("VLLM_USE_V2_MODEL_RUNNER", "1")
        .args([
            "-m",
            "foretoken_dt.serve",
            "--role",
            config
                .launch
                .dt
                .as_ref()
                .expect("DT launch selected")
                .role
                .as_str(),
        ])
        .arg(format!("--model={}", config.launch.artifacts.model))
        .arg(format!("--host={}", config.listen_address.ip()))
        .arg(format!("--port={}", config.listen_address.port()))
        .args(config.launch.render_vllm_args(None)?);
    if let Some(address) = config.dt_rdma_address {
        command.arg(format!("--rdma-host={address}"));
    }
    let engine = ManagedEngine::spawn(command, None).await?;
    let failure = tokio::select! {
        () = super::shutdown_signal() => None,
        status = engine.wait_for_exit() => Some(format!("DT role exited: {status}")),
        reason = super::wait_cache_server(cache_server) => Some(reason),
        error = super::wait_cache_write_failure(cache, mode) => Some(error.to_string()),
    };
    // Kubernetes normally closes admission first. Also honor direct process termination,
    // without delivering SIGTERM to EngineCore while it still owns active generation.
    let deadline = tokio::time::Instant::now() + config.launch.drain_timeout();
    if failure.is_none() {
        let address = if config.listen_address.ip().is_unspecified() {
            format!("127.0.0.1:{}", config.listen_address.port())
        } else {
            config.listen_address.to_string()
        };
        let endpoint = format!("http://{address}");
        let drain = drain(&endpoint);
        match tokio::time::timeout_at(deadline, drain).await {
            Ok(Ok(())) => {}
            Ok(Err(error)) => warn!(%error, "DT admission drain failed"),
            Err(_) => warn!("DT sessions did not drain before deadline"),
        }
    }
    cache_shutdown.notify_waiters();
    engine
        .shutdown(deadline.saturating_duration_since(tokio::time::Instant::now()))
        .await?;
    match failure {
        Some(reason) => Err(io::Error::other(reason).into()),
        None => Ok(()),
    }
}

// The role owns sessions and transfer storage; shutdown must wait for both.
async fn drain(endpoint: &str) -> Result<(), reqwest::Error> {
    let client = reqwest::Client::new();
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
