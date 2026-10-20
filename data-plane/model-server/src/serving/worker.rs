// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Supervises a non-ingress member's local engine, cache observer, and diagnostic control.

use std::future::IntoFuture;
use std::io;
use std::time::Instant;

use foretoken_model_server::backend::vllm::config::RuntimeConfig;
use foretoken_model_server::backend::vllm::startup::{
    EngineStartupFailure, spawn_engine_attempt, wait_cache_write_failure,
};
use foretoken_model_server::{profiling, runtime_cache, shutdown_signal};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tracing::warn;

/// Own a worker's child and local services; the leader establishes collective request readiness.
pub(super) async fn run(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
    cache_shutdown: CancellationToken,
) -> Result<(), Box<dyn std::error::Error>> {
    let listener = TcpListener::bind(config.listen_address).await?;
    let engine = spawn_engine_attempt(
        config,
        cache,
        profiling,
        runtime_cache::Mode::Persistent,
        Instant::now() + config.launch.startup_timeout(),
    )
    .await
    .map_err(EngineStartupFailure::into_error)?
    .engine;
    let mut profiler = profiling
        .filter(|_| config.launch.profiling.engine == profiling::Engine::Nsight)
        .cloned()
        .map(|profile| profiling::Supervisor::new(profile, None));
    let shutdown = CancellationToken::new();
    let server_shutdown = shutdown.clone();
    let app = axum::Router::new()
        .route(
            "/healthz",
            axum::routing::get(|| async { axum::http::StatusCode::OK }),
        )
        .route(
            "/readyz",
            axum::routing::get(|| async { axum::http::StatusCode::OK }),
        )
        .merge(profiling::router(
            profiler.as_ref().map(profiling::Supervisor::handle),
        ));
    let mut server = Box::pin(
        axum::serve(listener, app)
            .with_graceful_shutdown(server_shutdown.cancelled_owned())
            .into_future(),
    );
    let mut server_stopped = false;
    let failure = tokio::select! {
        signal = shutdown_signal() => signal.err().map(|error| format!("shutdown signal handler failed: {error}")),
        status = engine.wait_for_exit() => Some(format!("managed worker exited: {status}")),
        result = &mut server => {
            server_stopped = true;
            Some(format!("worker health server stopped: {result:?}"))
        },
        reason = runtime_cache::wait_for_server(cache_server) => Some(reason),
        error = wait_cache_write_failure(cache, runtime_cache::Mode::Persistent) => Some(error.to_string()),
        result = async {
            match &mut profiler {
                Some(profiler) => profiler.run().await,
                None => std::future::pending().await,
            }
        } => Some(result.err().unwrap_or_else(|| "capture supervisor stopped".into())),
    };

    // Diagnostics close before process retirement; observations cannot outlive this member.
    if let Some(profiler) = &profiler {
        profiler.handle().close();
    }
    shutdown.cancel();
    cache_shutdown.cancel();
    let deadline = Instant::now() + config.launch.drain_timeout();
    engine.shutdown(config.launch.drain_timeout()).await?;
    if let Some(profiler) = &mut profiler {
        profiler.engine_stopped("worker runtime terminated").await;
    }
    if !server_stopped {
        match tokio::time::timeout_at(deadline.into(), &mut server).await {
            Ok(Ok(())) => {}
            Ok(Err(error)) => warn!(%error, "worker health server failed while draining"),
            Err(_) => warn!("worker health server exceeded the drain deadline"),
        }
    }
    match failure {
        Some(message) => Err(io::Error::other(message).into()),
        None => Ok(()),
    }
}
