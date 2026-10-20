// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Composes process-local services before handing serving ownership to a leader or worker.

mod leader;
mod worker;

use std::sync::Arc;

use foretoken_model_server::backend::vllm::config::{MODEL_GROUP_UID_ENV, RuntimeConfig};
use foretoken_model_server::{profiling, runtime_cache};
use tokio::net::TcpListener;
use tokio::sync::Notify;

/// Resolve the published runtime inputs and supervise the current execution-group member.
pub(super) async fn run() -> Result<(), Box<dyn std::error::Error>> {
    // Resolve configuration before starting any engine or network task.
    let cache_config = runtime_cache::Config::from_env().map_err(std::io::Error::other)?;
    let config = RuntimeConfig::from_env(
        cache_config
            .as_ref()
            .map(runtime_cache::Config::observation_port),
    )
    .map_err(std::io::Error::other)?;
    let request_ledger = foretoken_request_ledger::RequestLedger::from_env()?.map(Arc::new);
    let cache_shutdown = Arc::new(Notify::new());
    let profiling_config = if let Some(cache) = &cache_config {
        let workers = config.launch.parallelism.tp
            * config.launch.parallelism.pp
            * config.launch.parallelism.dp
            * config.launch.parallelism.pcp;
        Some(
            profiling::Config::from_runtime_cache(
                cache,
                required_env(MODEL_GROUP_UID_ENV)?,
                workers,
                config.launch.profiling.engine,
                foretoken_model_server::engine::python_executable(),
            )
            .map_err(std::io::Error::other)?,
        )
    } else {
        None
    };
    let mut cache_server = if let Some(server_config) = cache_config.clone() {
        let address = (config.listen_address.ip(), server_config.observation_port());
        let listener = TcpListener::bind(address).await?;
        let shutdown = cache_shutdown.clone();
        Some(tokio::spawn(async move {
            runtime_cache::serve(listener, server_config, shutdown).await
        }))
    } else {
        None
    };

    // Workers own local execution only; the leader owns the group handshake and request ingress.
    if config
        .member
        .as_ref()
        .is_some_and(|member| member.index != 0)
    {
        return worker::run(
            &config,
            cache_config.as_ref(),
            profiling_config.as_ref(),
            &mut cache_server,
            cache_shutdown,
        )
        .await;
    }
    leader::Leader::start(
        config,
        cache_config,
        profiling_config,
        request_ledger,
        cache_server,
        cache_shutdown,
    )
    .await?
    .serve()
    .await
}

fn required_env(name: &str) -> Result<String, Box<dyn std::error::Error>> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{name} must be set by the ModelGroup controller").into())
}
