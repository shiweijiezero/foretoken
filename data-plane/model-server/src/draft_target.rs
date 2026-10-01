// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Supervises the installed DT role application with the normal model-server process owner.

use std::io;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Arc;
use std::time::Duration;

use foretoken_model_server::{
    config::RuntimeConfig, launch::PYTHON_MODULE_PATH, managed_engine::ManagedEngine, preparation,
    runtime_cache,
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
    let artifacts = &config.launch.artifacts;
    let mut model = artifacts.model.clone();
    let mut arguments = config.launch.render_vllm_args(None)?;
    let mut prepared_tokenizer = None;
    let scope = std::env::var(preparation::PREPARATION_SCOPE_ENV).ok();
    let legacy_modelscope =
        scope.is_none() && artifacts.source == foretoken_artifacts::ModelSource::ModelScope;
    environment.push(("VLLM_USE_MODELSCOPE".into(), legacy_modelscope.to_string()));
    if legacy_modelscope {
        environment.push((
            foretoken_artifacts::MODELSCOPE_CACHE_ENV.into(),
            foretoken_artifacts::modelscope_cache_root(&model_root)
                .display()
                .to_string(),
        ));
    }
    let tokenizer = if artifacts.source == foretoken_artifacts::ModelSource::Local {
        model = super::local_artifact_path(&artifacts.model)?;
        Some(super::local_artifact_path(&artifacts.tokenizer)?)
    } else if let Some(scope) = scope {
        let prepared = tokio::time::timeout(
            config.launch.startup_timeout(),
            preparation::prepare(&config.launch, &model_root, &scope, true, None),
        )
        .await??;
        model = prepared.model.display().to_string();
        let binding = cache
            .filter(|cache| prepared.tokenizer.starts_with(cache.model_root(mode)))
            .and_then(|_| std::env::var(foretoken_artifacts::RUNTIME_CACHE_BINDING_ENV).ok());
        prepared_tokenizer = Some(prepared.tokenizer_metadata(&scope, binding));
        Some(prepared.tokenizer.display().to_string())
    } else {
        None
    };
    if let Some(tokenizer) = tokenizer {
        arguments.retain(|arg| {
            !arg.starts_with("--revision=") && !arg.starts_with("--tokenizer-revision=")
        });
        for argument in &mut arguments {
            if argument.starts_with("--tokenizer=") {
                *argument = format!("--tokenizer={tokenizer}");
            }
        }
    }
    // Engine paths are materialized snapshots; discovery retains the configured source identity.
    environment.push(("FORETOKEN_DT_MODEL_METADATA".into(), serde_json::json!({
        "model": artifacts.model,
        "revision": (artifacts.source != foretoken_artifacts::ModelSource::Local).then_some(&artifacts.revision),
        "tokenizer": artifacts.tokenizer,
        "tokenizer_revision": (artifacts.source != foretoken_artifacts::ModelSource::Local).then_some(&artifacts.tokenizer_revision),
        "prepared_tokenizer": prepared_tokenizer,
    }).to_string()));
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
        .arg(format!("--model={model}"))
        .arg(format!("--host={}", config.listen_address.ip()))
        .arg(format!("--port={}", config.listen_address.port()))
        .args(arguments);
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
        .shutdown_application(deadline.saturating_duration_since(tokio::time::Instant::now()))
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
