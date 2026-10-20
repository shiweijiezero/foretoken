// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns instance acceptance, projected limits, and execution-termination observations.

use std::num::{NonZeroU32, NonZeroU64};
use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// One Pool's versioned acceptance limit, shared by its ingress processes.
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct InstanceAdmissionConfig {
    pub version: NonZeroU64,
    // Unlimited is explicit null; an incomplete snapshot must not remove a live cap.
    #[serde(deserialize_with = "Option::deserialize")]
    pub max_concurrent_requests: Option<NonZeroU32>,
}

/// Atomic configuration and occupancy observation returned to the controller.
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

/// Engine state used for API health and admission only; process ownership stays upstream.
#[derive(Default)]
pub struct RuntimeHealth {
    process_alive: AtomicBool,
    client_healthy: AtomicBool,
    // Acceptance, configuration publication, and close share one linearization boundary.
    admission: Mutex<InstanceAdmissionStatus>,
    failure: tokio::sync::Notify,
    drained: tokio::sync::Notify,
    stopped: tokio::sync::Notify,
}

/// A definite rejection before any request is submitted to the inference engine.
#[derive(Debug, Clone, Copy)]
pub enum AdmissionRejection {
    Closed,
    Busy,
}

/// Instance slot retained until engine termination is confirmed.
pub struct AdmissionPermit {
    health: Arc<RuntimeHealth>,
}

impl RuntimeHealth {
    /// Creates closed health and admission state for the model-server supervisor.
    pub fn new() -> Self {
        Self::default()
    }

    /// Initializes a protocol-0 startup limit before the supervisor opens ingress.
    /// This does not acknowledge support for projected live configuration.
    pub fn set_startup_admission_limit(&self, limit: Option<u32>) {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .max_concurrent_requests = limit;
    }

    /// Applies a newer or identical configuration without resetting occupancy or reopening admission.
    /// The configuration watcher calls this before acknowledging the controller's version.
    pub fn apply_instance_configuration(&self, configuration: InstanceAdmissionConfig) {
        let mut state = self
            .admission
            .lock()
            .expect("instance admission lock poisoned");
        let version = configuration.version.get();
        let limit = configuration
            .max_concurrent_requests
            .map(|limit| limit.get());
        if state.active_generation.is_some_and(|active| {
            version < active || version == active && state.max_concurrent_requests != limit
        }) {
            state.target_generation = Some(version);
            state.configuration_error =
                Some("instance admission version is stale or has different limits".into());
            return;
        }
        state.max_concurrent_requests = limit;
        state.active_generation = Some(version);
        state.target_generation = Some(version);
        state.configuration_error = None;
    }

    /// Reports a rejected candidate while retaining the last applied limit and permits.
    pub fn reject_instance_configuration(&self, version: Option<u64>, error: String) {
        let mut state = self
            .admission
            .lock()
            .expect("instance admission lock poisoned");
        if let Some(version) = version {
            state.target_generation = Some(version);
        }
        state.configuration_error = Some(error);
    }

    /// Returns one coherent configuration and occupancy observation for the controller.
    pub fn instance_admission_status(&self) -> InstanceAdmissionStatus {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .clone()
    }

    /// Requests supervised engine shutdown when a submitted request's completion becomes unknown.
    pub fn fail_execution(&self) {
        self.set_accepting(false);
        self.set_client_healthy(false);
        self.failure.notify_one();
    }

    /// Waits for an execution-lifecycle failure requiring the supervisor to stop the engine.
    pub async fn execution_failed(&self) {
        self.failure.notified().await;
    }

    /// Waits until every accepted execution has relinquished its instance slot.
    pub async fn wait_drained(&self) {
        loop {
            let drained = self.drained.notified();
            tokio::pin!(drained);
            drained.as_mut().enable();
            if self.running_requests() == 0 {
                return;
            }
            drained.await;
        }
    }

    /// Publishes managed-engine process liveness to probe handlers; the supervisor owns updates.
    pub fn set_process_alive(&self, value: bool) {
        self.process_alive.store(value, Ordering::Release);
        if !value {
            self.stopped.notify_waiters();
        }
    }

    /// Reports native-process liveness to source-cancellation handlers while admission is closed.
    pub(crate) fn process_alive(&self) -> bool {
        self.process_alive.load(Ordering::Acquire)
    }

    /// Waits for the supervisor's confirmation that the managed engine has stopped.
    pub async fn wait_stopped(&self) {
        loop {
            let stopped = self.stopped.notified();
            tokio::pin!(stopped);
            stopped.as_mut().enable();
            if !self.process_alive.load(Ordering::Acquire) {
                return;
            }
            stopped.await;
        }
    }

    /// Publishes EngineCore client health to probe handlers; the supervisor owns updates.
    pub fn set_client_healthy(&self, value: bool) {
        self.client_healthy.store(value, Ordering::Release);
    }

    /// Opens or closes new HTTP admission; accepted executions retain their instance slots.
    pub fn set_accepting(&self, value: bool) {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .accepting = value;
    }

    /// Returns process-and-client health for the `/healthz` probe without changing lifecycle state.
    pub fn healthy(&self) -> bool {
        self.process_alive.load(Ordering::Acquire) && self.client_healthy.load(Ordering::Acquire)
    }

    /// Returns readiness for the `/readyz` probe; it currently shares the health lifecycle signal.
    pub fn ready(&self) -> bool {
        self.healthy()
    }

    /// Returns whether new requests may enter for telemetry and admission consumers.
    ///
    /// Execution tasks retain existing permits after this signal closes.
    pub fn accepting(&self) -> bool {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .accepting
    }

    /// Reserves an instance slot against the current limit; the execution task owns its release.
    pub fn try_admit(self: &Arc<Self>) -> Result<AdmissionPermit, AdmissionRejection> {
        let mut state = self
            .admission
            .lock()
            .expect("instance admission lock poisoned");
        if !state.accepting {
            return Err(AdmissionRejection::Closed);
        }
        if state
            .max_concurrent_requests
            .is_some_and(|limit| state.running_requests >= u64::from(limit))
        {
            return Err(AdmissionRejection::Busy);
        }
        state.running_requests += 1;
        Ok(AdmissionPermit {
            health: self.clone(),
        })
    }

    /// Returns the number of accepted requests still holding a permit.
    pub fn running_requests(&self) -> u64 {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .running_requests
    }
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

impl Drop for AdmissionPermit {
    fn drop(&mut self) {
        self.health
            .admission
            .lock()
            .expect("instance admission lock poisoned")
            .running_requests -= 1;
        self.health.drained.notify_waiters();
    }
}
