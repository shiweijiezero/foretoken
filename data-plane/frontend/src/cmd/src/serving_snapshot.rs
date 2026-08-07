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
    mut installed_serving_snapshot: Option<Vec<u8>>,
    builder: Arc<RuntimeBuilder>,
) {
    let mut read_failure_reported = false;
    loop {
        match std::fs::read(&path) {
            Ok(bytes) if installed_serving_snapshot.as_ref() == Some(&bytes) => {
                read_failure_reported = false;
            }
            Ok(bytes) => {
                read_failure_reported = false;
                let snapshot = match builder.parse(&bytes) {
                    Ok(snapshot) => snapshot,
                    Err(error) => {
                        tracing::error!(%error, "could not parse serving snapshot candidate");
                        tokio::time::sleep(Duration::from_secs(1)).await;
                        continue;
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
                    installed_serving_snapshot = Some(bytes);
                    tokio::time::sleep(Duration::from_secs(1)).await;
                    continue;
                }
                match builder.build(snapshot).await {
                    Ok(prepared) => match snapshot_is_current(&path, &bytes) {
                        Ok(true) => {
                            if prepared.publish(&generation) {
                                installed_serving_snapshot = Some(bytes);
                            }
                        }
                        Ok(_) => tracing::info!(
                            candidate_version,
                            "discarding serving snapshot candidate superseded during preparation"
                        ),
                        Err(error) => tracing::error!(
                            path = %path.display(),
                            %error,
                            "serving snapshot became unreadable before publication"
                        ),
                    },
                    Err(error) => {
                        tracing::warn!(%error, "could not prepare serving snapshot candidate")
                    }
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

fn snapshot_is_current(path: &std::path::Path, expected: &[u8]) -> std::io::Result<bool> {
    std::fs::read(path).map(|current| current == expected)
}

#[cfg(test)]
pub(crate) async fn install_serving_snapshot(
    generation: &RuntimeGeneration,
    bytes: &[u8],
    builder: &RuntimeBuilder,
) -> bool {
    let snapshot = match builder.parse(bytes) {
        Ok(snapshot) => snapshot,
        Err(error) => {
            tracing::error!(%error, "could not parse serving snapshot");
            return false;
        }
    };
    let version = snapshot.version;
    if generation
        .active_version()
        .is_some_and(|active| active >= version)
    {
        tracing::warn!(version, "ignoring stale serving snapshot candidate");
        return false;
    }
    match builder.build(snapshot).await {
        Ok(prepared) => prepared.publish(generation),
        Err(error) => {
            tracing::warn!(%error, "could not prepare serving snapshot");
            false
        }
    }
}

#[cfg(test)]
#[path = "tests/model_identities.rs"]
mod tests;
