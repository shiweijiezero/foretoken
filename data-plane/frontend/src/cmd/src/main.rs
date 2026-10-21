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
use tokio_util::sync::CancellationToken;
use tokio_util::task::TaskTracker;

/// Publishes controller snapshots to the HTTP frontend and owns admission-close and shutdown drain.
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    foretoken_artifacts::source::activate("foretoken-frontend")?;
    let logging = Arc::new(foretoken_tracing::LogControl::new("ForetokenFrontend")?);

    // Establish the long-lived generation owner before starting background refreshes.
    // Snapshot updates publish atomically, so an invalid update cannot replace active routing.
    let config = RuntimeConfig::from_env().map_err(std::io::Error::other)?;

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
    // Capacity is shared by service identity; opening the store remains lazy so an
    // unrestricted service does not depend on its availability. Never log the store URL.
    let ledger = foretoken_request_ledger::RequestLedger::from_env()?.map(Arc::new);
    let admission = Arc::new(foretoken_admission::AdmissionRegistry::new(
        config.frontend_uid,
        config.pod_uid,
        ledger.clone(),
    ));
    let builder = Arc::new(RuntimeBuilder::new(kv_credential, logging));
    let generation = Arc::new(RuntimeGeneration::new(admission));

    // Bind the HTTP listener before launching the refresh loops. The process can remain
    // live while readiness stays false until a valid routing snapshot is published.
    let listener = tokio::net::TcpListener::bind(config.listen_address).await?;
    let model_generation = generation.clone();
    let models = Arc::new(move || model_generation.configured_models());
    let (app, response_tasks) = router(generation.clone(), models, ledger)?;
    let shutdown = CancellationToken::new();
    let background = TaskTracker::new();
    background.spawn(
        shutdown
            .child_token()
            .run_until_cancelled_owned(refresh_active_generation(generation.clone())),
    );
    background.spawn(
        shutdown
            .child_token()
            .run_until_cancelled_owned(watch_serving_snapshot(
                generation.clone(),
                config.serving_snapshot,
                None,
                builder,
            )),
    );
    let mut server = Box::pin(
        axum::serve(listener, app)
            .with_graceful_shutdown(shutdown.clone().cancelled_owned())
            .into_future(),
    );

    // A server failure exits after ancillary work stops; signals also drain accepted requests.
    let stopped = tokio::select! {
        result = &mut server => Some(result),
        () = shutdown_signal() => None,
    };

    // Reject new requests before stopping HTTP and snapshot work. Cancellation remains visible
    // even when Axum has not yet polled its graceful-shutdown future.
    generation.close_admission();
    shutdown.cancel();
    background.close();
    if let Some(result) = stopped {
        background.wait().await;
        result?;
        return Ok(());
    }
    let drained = tokio::time::timeout(config.drain, async {
        let (served, (), ()) =
            tokio::join!(server.as_mut(), response_tasks.drain(), background.wait());
        served
    })
    .await;
    match drained {
        Ok(result) => result?,
        Err(_) => {
            response_tasks.cancel();
            tracing::warn!("frontend requests did not drain before the shutdown deadline");
        }
    }
    Ok(())
}

/// Waits for process termination before main closes admission and drains accepted work.
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
