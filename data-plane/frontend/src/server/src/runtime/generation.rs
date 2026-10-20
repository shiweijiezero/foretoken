// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Publishes runtime generations while retaining one admission and configuration owner.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use arc_swap::ArcSwapOption;
use foretoken_admission::{Admission, AdmissionRegistry, PreparedAdmissions};

use super::{GenerationError, RuntimeControl, RuntimeState};

/// A complete published generation runtime.
///
/// State and control originate from the same routing snapshot and must therefore be read as one
/// atomic slot.
pub(super) struct RuntimeSlot {
    pub(super) version: u64,
    pub(super) models: Arc<[String]>,
    pub(super) state: Arc<RuntimeState>,
    pub(super) control: Arc<dyn RuntimeControl>,
}

#[derive(Default, Clone)]
pub(super) struct ConfigurationStatus {
    pub(super) target_generation: Option<u64>,
    pub(super) error: Option<String>,
}

/// Owns atomic runtime publication and request-scoped views of the active generation.
#[derive(Clone)]
pub struct RuntimeGeneration {
    pub(super) slot: Arc<ArcSwapOption<RuntimeSlot>>,
    pub(super) publication: Arc<Mutex<()>>,
    pub(super) publication_updates: tokio::sync::watch::Sender<u64>,
    pub(super) accepting: Arc<AtomicBool>,
    pub(super) request_slot: Option<Arc<RuntimeSlot>>,
    pub(super) configuration: Arc<Mutex<ConfigurationStatus>>,
    pub(super) admission: Arc<AdmissionRegistry>,
}

impl RuntimeGeneration {
    /// Creates the long-lived owner of controller-published runtime generations.
    ///
    /// The frontend process retains the returned owner for its full lifetime; snapshot watchers
    /// publish into it and HTTP handlers load its current immutable state per request.
    pub fn new(admission: Arc<AdmissionRegistry>) -> Self {
        let (publication_updates, _) = tokio::sync::watch::channel(0);
        Self {
            slot: Arc::new(ArcSwapOption::empty()),
            publication: Arc::new(Mutex::new(())),
            publication_updates,
            accepting: Arc::new(AtomicBool::new(true)),
            request_slot: None,
            configuration: Arc::default(),
            admission,
        }
    }

    /// Publishes a newer serving generation without allowing concurrent stale writers to win.
    pub fn replace_state(
        &self,
        version: u64,
        state: Arc<RuntimeState>,
        control: Arc<dyn RuntimeControl>,
        admission: PreparedAdmissions,
    ) -> bool {
        let _publication = self
            .publication
            .lock()
            .expect("runtime publication lock poisoned");
        if self
            .slot
            .load_full()
            .is_some_and(|active| active.version >= version)
        {
            return false;
        }
        if !self.accepting.load(Ordering::Acquire) {
            return false;
        }
        control.activate(version);
        self.admission.publish(version, admission);
        self.slot.store(Some(Arc::new(RuntimeSlot {
            version,
            models: control.configured_models().into(),
            state,
            control,
        })));
        self.record_configuration(Some(version), None);
        self.publication_updates.send_replace(version);
        true
    }

    /// Advances controller-observed Pod ownership without activating candidate serving settings.
    /// Snapshot watchers call this before preparation, including candidates rejected by Router.
    pub fn publish_admission_membership(
        &self,
        version: u64,
        models: Vec<String>,
        frontend_instances: &[String],
        backend_instances: &[String],
    ) {
        self.admission
            .publish_membership(version, models, frontend_instances, backend_instances);
    }

    /// Records watcher progress without changing the last valid request-processing state.
    pub fn record_configuration(&self, version: Option<u64>, error: Option<String>) {
        let mut status = self
            .configuration
            .lock()
            .expect("configuration status lock poisoned");
        if let Some(version) = version {
            status.target_generation = Some(version);
        }
        status.error = error;
    }

    /// Stops accepting new requests while retaining the active generation for draining streams.
    ///
    /// Process shutdown calls this once before stopping HTTP; it wakes admission waiters, which
    /// then return unavailable while requests already holding a runtime can finish.
    pub fn close_admission(&self) {
        self.accepting.store(false, Ordering::Release);
        self.admission.close();
        self.publication_updates
            .send_replace(self.slot.load_full().map_or(0, |slot| slot.version));
    }

    /// Refreshes readiness for the currently loaded runtime slot and wakes queued admission checks.
    pub async fn refresh_backend_readiness(&self) {
        if let Some(slot) = self.slot.load_full() {
            slot.control.refresh_backend_readiness().await;
            self.publication_updates.send_replace(slot.version);
        }
    }

    /// Returns the version of the generation currently published by the snapshot watcher.
    ///
    /// Snapshot processing uses the optional value to reject stale candidates. `None` indicates
    /// that the frontend has not yet published a serving generation.
    pub fn active_version(&self) -> Option<u64> {
        self.slot.load_full().map(|slot| slot.version)
    }

    /// Returns an owned model list for the currently loaded runtime slot, or an empty list before publication.
    pub fn configured_models(&self) -> Vec<String> {
        self.slot
            .load_full()
            .map(|slot| slot.control.configured_models())
            .unwrap_or_default()
    }

    /// Reads the request's captured budget, or the active budget before a snapshot is captured.
    pub(super) fn request_timeout(&self) -> Result<Duration, GenerationError> {
        Ok(self
            .request_slot
            .as_ref()
            .cloned()
            .or_else(|| self.slot.load_full())
            .ok_or(GenerationError::Unavailable)?
            .state
            .request_timeout)
    }

    /// Selects a configured model's rule without creating state from request-supplied names.
    pub(super) fn model_admission(&self, model: &str) -> Result<Arc<Admission>, GenerationError> {
        let _publication = self
            .publication
            .lock()
            .expect("runtime publication lock poisoned");
        if !self.accepting.load(Ordering::Acquire) {
            return Err(GenerationError::Unavailable);
        }
        self.admission.model(model).ok_or_else(|| {
            if self
                .configured_models()
                .iter()
                .any(|configured| configured == model)
            {
                GenerationError::Unavailable
            } else {
                GenerationError::ModelNotFound
            }
        })
    }

    /// Loads a ready slot for request execution under the same boundary as publication and close.
    pub(super) fn ready_state(&self) -> Result<Arc<RuntimeSlot>, GenerationError> {
        let _publication = self
            .publication
            .lock()
            .expect("runtime publication lock poisoned");
        if !self.accepting.load(Ordering::Acquire) {
            return Err(GenerationError::Unavailable);
        }
        let slot = self.slot.load_full().ok_or(GenerationError::Unavailable)?;
        if slot.control.is_ready() {
            Ok(slot)
        } else {
            Err(GenerationError::Unavailable)
        }
    }
}
