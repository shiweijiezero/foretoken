// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Adopts controller-published instance limits without replacing execution ownership.

use std::num::{NonZeroU32, NonZeroU64};
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use serde::{Deserialize, Serialize};

use crate::api::RuntimeHealth;

/// One Pool's versioned acceptance limit, shared by its ingress processes.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstanceAdmissionConfig {
    pub version: NonZeroU64,
    // Unlimited is explicit null; an incomplete snapshot must not remove a live cap.
    #[serde(deserialize_with = "Option::deserialize")]
    pub max_concurrent_requests: Option<NonZeroU32>,
}

/// Atomic configuration and occupancy observation returned by the internal admission endpoint.
#[derive(Clone, Debug, Default, Serialize)]
pub struct InstanceAdmissionStatus {
    pub active_generation: Option<u64>,
    pub target_generation: Option<u64>,
    pub configuration_error: Option<String>,
    pub max_concurrent_requests: Option<u32>,
    pub running_requests: u64,
    pub accepting: bool,
}

/// Process-owned watcher for a projected Pool configuration; accepted executions retain their permits.
pub struct InstanceAdmissionWatcher {
    path: PathBuf,
    health: Arc<RuntimeHealth>,
    last_processed: Vec<u8>,
}

impl InstanceAdmissionWatcher {
    /// Validates and applies initial limits before bootstrap may open the ingress.
    pub fn load(path: PathBuf, health: Arc<RuntimeHealth>) -> Result<Self, std::io::Error> {
        let bytes = std::fs::read(&path)?;
        let configuration = serde_json::from_slice(&bytes).map_err(std::io::Error::other)?;
        health.apply_instance_configuration(configuration);
        Ok(Self {
            path,
            health,
            last_processed: bytes,
        })
    }

    /// Watches for changed input until the model-server supervisor drops this future.
    /// Invalid candidates leave the applied limit and outstanding executions unchanged.
    pub async fn run(mut self) -> std::convert::Infallible {
        let mut read_failure_reported = false;
        loop {
            match std::fs::read(&self.path) {
                Ok(bytes) => {
                    read_failure_reported = false;
                    if bytes != self.last_processed {
                        match serde_json::from_slice::<InstanceAdmissionConfig>(&bytes) {
                            Ok(configuration) => {
                                self.health.apply_instance_configuration(configuration);
                            }
                            Err(error) => {
                                let version = serde_json::from_slice::<serde_json::Value>(&bytes)
                                    .ok()
                                    .and_then(|value| {
                                        value.get("version").and_then(serde_json::Value::as_u64)
                                    });
                                self.health
                                    .reject_instance_configuration(version, error.to_string());
                                tracing::warn!(%error, "instance admission configuration rejected; retaining active limits");
                            }
                        }
                        self.last_processed = bytes;
                    }
                }
                Err(error) => {
                    if !read_failure_reported {
                        tracing::error!(path = %self.path.display(), %error, "instance admission configuration is unreadable; retaining active limits");
                        read_failure_reported = true;
                    }
                }
            }
            tokio::time::sleep(Duration::from_secs(1)).await;
        }
    }
}
