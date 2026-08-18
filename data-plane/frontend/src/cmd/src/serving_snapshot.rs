// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Watches serving snapshots and atomically publishes prepared runtime generations.

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use foretoken_runtime_builder::RuntimeBuilder;
use foretoken_server::RuntimeGeneration;

pub(crate) async fn refresh_active_generation(generation: Arc<RuntimeGeneration>) {
    loop {
        tokio::time::sleep(Duration::from_secs(1)).await;
        generation.refresh_backend_readiness().await;
    }
}

pub(crate) async fn watch_serving_snapshot(
    generation: Arc<RuntimeGeneration>,
    path: PathBuf,
    mut last_processed_snapshot: Option<Vec<u8>>,
    builder: Arc<RuntimeBuilder>,
) {
    let mut read_failure_reported = false;
    loop {
        match std::fs::read(&path) {
            Ok(bytes) if last_processed_snapshot.as_ref() == Some(&bytes) => {
                read_failure_reported = false;
            }
            Ok(bytes) => {
                read_failure_reported = false;
                if process_serving_snapshot(&generation, &bytes, &builder).await {
                    last_processed_snapshot = Some(bytes);
                }
            }
            Err(error) => {
                if !read_failure_reported {
                    tracing::error!(path = %path.display(), %error, "serving snapshot is unreadable; keeping the active generation");
                    read_failure_reported = true;
                }
            }
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}

async fn process_serving_snapshot(
    generation: &RuntimeGeneration,
    bytes: &[u8],
    builder: &RuntimeBuilder,
) -> bool {
    let snapshot = match builder.parse(bytes) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            tracing::error!(%error, "could not parse serving snapshot candidate");
            return false;
        }
    };
    let candidate_version = snapshot.version;
    if generation
        .active_version()
        .is_some_and(|active| active >= candidate_version)
    {
        tracing::warn!(
            candidate_version,
            "ignoring stale serving snapshot candidate"
        );
        return true;
    }
    match builder.build(snapshot).await {
        Ok(prepared) => prepared.publish(generation),
        Err(error) => {
            tracing::warn!(%error, "could not prepare serving snapshot candidate");
            false
        }
    }
}
