// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Watches serving snapshots and atomically publishes prepared runtime generations.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use foretoken_runtime_builder::{RuntimeBuildError, RuntimeBuilder};
use foretoken_server::RuntimeGeneration;

/// Periodically refreshes backend readiness for the active generation.
///
/// `main` spawns this loop for the frontend process lifetime. It updates control state in place so
/// readiness can recover without replacing the immutable request-processing generation.
pub(crate) async fn refresh_active_generation(generation: Arc<RuntimeGeneration>) {
    loop {
        tokio::time::sleep(Duration::from_secs(1)).await;
        generation.refresh_backend_readiness().await;
    }
}

/// Watches controller snapshots and publishes each newly prepared runtime generation.
///
/// `main` retains this loop for process lifetime. Invalid rules wait for changed input;
/// unavailable backends remain retryable while the active generation continues serving.
pub(crate) async fn watch_serving_snapshot(
    generation: Arc<RuntimeGeneration>,
    path: PathBuf,
    mut last_processed_snapshot: Option<Vec<u8>>,
    builder: Arc<RuntimeBuilder>,
) {
    let mut read_failure_reported = false;
    let mut last_preparation_error = None;

    // Remember completed or rejected input; retry only preparation that can recover unchanged.
    loop {
        match std::fs::read(&path) {
            Ok(bytes) => {
                if read_failure_reported {
                    tracing::info!("serving snapshot is readable again");
                    read_failure_reported = false;
                }
                if last_processed_snapshot.as_ref() != Some(&bytes)
                    && process_serving_snapshot(
                        &generation,
                        &path,
                        &bytes,
                        &builder,
                        &mut last_preparation_error,
                    )
                    .await
                {
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

/// Prepares the current snapshot and reports whether the watcher can mark its input as processed.
async fn process_serving_snapshot(
    generation: &RuntimeGeneration,
    path: &Path,
    bytes: &[u8],
    builder: &RuntimeBuilder,
    last_preparation_error: &mut Option<(u64, String)>,
) -> bool {
    let snapshot = match builder.parse(bytes) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            // A rejected field can still belong to an identifiable controller publication.
            let version = serde_json::from_slice::<serde_json::Value>(bytes)
                .ok()
                .and_then(|value| value.get("version").and_then(serde_json::Value::as_u64));
            generation.record_configuration(version, Some(error.to_string()));
            tracing::error!(%error, "could not parse serving snapshot candidate");
            return true;
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
    generation.publish_admission_membership(
        candidate_version,
        snapshot.admission.keys().cloned().collect(),
        &snapshot.frontend_instances,
        &snapshot.backend_instances,
    );
    generation.record_configuration(Some(candidate_version), None);
    match builder.build(snapshot).await {
        Ok(prepared) => match std::fs::read(path) {
            Ok(current) if current == bytes => {
                let published = prepared.publish(generation);
                if published {
                    let recovered = last_preparation_error.take().is_some();
                    tracing::info!(
                        candidate_version,
                        models = generation.configured_models().len(),
                        recovered,
                        "serving configuration applied"
                    );
                }
                published
            }
            Ok(_) => {
                tracing::debug!(
                    candidate_version,
                    "serving snapshot candidate was superseded during preparation"
                );
                true
            }
            Err(error) => {
                tracing::warn!(path = %path.display(), %error, "could not confirm serving snapshot candidate before publication");
                false
            }
        },
        Err(error) => {
            let rejected = matches!(
                &error,
                RuntimeBuildError::Router(_) | RuntimeBuildError::InvalidSnapshot(_)
            );
            generation
                .record_configuration(Some(candidate_version), rejected.then(|| error.to_string()));
            let failure = (candidate_version, error.to_string());
            if last_preparation_error.as_ref() != Some(&failure) {
                tracing::warn!(candidate_version, %error, "could not prepare serving snapshot candidate; keeping the active generation");
                *last_preparation_error = Some(failure);
            }
            rejected
        }
    }
}
