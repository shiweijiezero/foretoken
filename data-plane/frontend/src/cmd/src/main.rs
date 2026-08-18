// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Starts a frontend from its controller-mounted routing snapshot.

mod config;
mod serving_snapshot;

use std::sync::Arc;

use config::{KvIndexKeyError, RuntimeConfig, kv_index_key};
use foretoken_kv_indexer::KvIndexDegradedReason;
use foretoken_runtime_builder::{KvIndexCredential, RuntimeBuilder};
use foretoken_server::{RuntimeGeneration, router};
use serving_snapshot::{refresh_active_generation, watch_serving_snapshot};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    foretoken_tracing::init_tracing("ForetokenFrontend");
    let config = RuntimeConfig::from_env().map_err(std::io::Error::other)?;
    let generation = Arc::new(RuntimeGeneration::new());
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
    let app = router(generation, models, config.stream_idle);
    axum::serve(listener, app).await?;
    Ok(())
}
