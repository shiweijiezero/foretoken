// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Starts a frontend from its controller-mounted routing snapshot.

mod config;
mod serving_snapshot;

use std::future::IntoFuture;
use std::sync::Arc;

use config::{KvIndexKeyError, RuntimeConfig, kv_index_key};
use foretoken_kv_indexer::KvIndexDegradedReason;
use foretoken_runtime_builder::{KvIndexCredential, RuntimeBuilder};
use foretoken_server::{RuntimeGeneration, router};
use serving_snapshot::{refresh_active_generation, watch_serving_snapshot};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    foretoken_tracing::init_tracing("ForetokenFrontend");

    // Establish the long-lived generation owner before starting background refreshes.
    // Snapshot updates publish atomically, so an invalid update cannot replace active routing.
    let config = RuntimeConfig::from_env().map_err(std::io::Error::other)?;
    let generation = Arc::new(RuntimeGeneration::new(config.request_timeout));

    // KV locality is optional routing input. Credential failures degrade its score instead
    // of preventing otherwise healthy model routes from serving requests.
    let kv_credential = match kv_index_key() {
        Ok(Some(key)) => KvIndexCredential::Key(key),
        Ok(None) => KvIndexCredential::Disabled,
        Err(error) => {
            let reason = match error {
                KvIndexKeyError::ReadFailed => KvIndexDegradedReason::KeyReadFailed,
                KvIndexKeyError::InvalidLength => KvIndexDegradedReason::KeyInvalidLength,
            };
            tracing::error!(
                ?reason,
                "KV index credential is invalid; locality scoring is degraded"
            );
            KvIndexCredential::Degraded(reason)
        }
    };
    let builder = Arc::new(RuntimeBuilder::new(config.router_pipeline, kv_credential));

    // Bind the HTTP listener before launching the refresh loops. The process can remain
    // live while readiness stays false until a complete serving generation is available.
    let listener = tokio::net::TcpListener::bind(config.listen_address).await?;
    tokio::spawn(refresh_active_generation(generation.clone()));
    tokio::spawn(watch_serving_snapshot(
        generation.clone(),
        config.serving_snapshot,
        None,
        builder,
    ));
    let model_generation = generation.clone();
    let models = Arc::new(move || model_generation.configured_models());
    let app = router(generation.clone(), models, config.stream_idle);
    let shutdown = Arc::new(tokio::sync::Notify::new());
    let server_shutdown = shutdown.clone();
    let mut server = Box::pin(
        axum::serve(listener, app)
            .with_graceful_shutdown(async move { server_shutdown.notified().await })
            .into_future(),
    );

    // A server failure exits immediately; an operating-system shutdown follows the
    // admission-close and bounded drain sequence below.
    tokio::select! {
        result = &mut server => {
            result?;
            return Ok(());
        }
        () = shutdown_signal() => {}
    }

    // Reject new requests before stopping the HTTP server, then allow accepted streams
    // to finish within the same request budget used by the serving runtime.
    generation.close_admission();
    shutdown.notify_waiters();
    match tokio::time::timeout(config.request_timeout, server.as_mut()).await {
        Ok(result) => result?,
        Err(_) => tracing::warn!("frontend requests did not drain before the request timeout"),
    }
    Ok(())
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
