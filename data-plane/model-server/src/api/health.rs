// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Health, admission slots, and termination notifications shared with the process supervisor.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use crate::instance_admission::{InstanceAdmissionConfig, InstanceAdmissionStatus};

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
