// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Per-model admission publication with stable built-in capacity and custom-rule draining.

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex, Weak};

use crate::telemetry::{AdmissionMetricsScope, AdmissionModelMetrics, set_configured_models};
use crate::{
    AdmissionAttempt, AdmissionConfig, AdmissionConfigError, AdmissionContext, AdmissionError,
    AdmissionPermit, AdmissionRequest, AdmissionReservation, AdmissionRule, AdmissionService,
    AdmissionStateReader,
};

/// Validated rules prepared without publishing metrics or changing active requests.
pub struct PreparedAdmissions(BTreeMap<String, PreparedRule>);

impl PreparedAdmissions {
    /// Constructs a candidate independently of live rules, which may close before publication.
    pub fn new(configs: &BTreeMap<String, AdmissionConfig>) -> Result<Self, AdmissionConfigError> {
        configs
            .iter()
            .map(|(model, config)| config.prepare().map(|rule| (model.clone(), rule)))
            .collect::<Result<BTreeMap<_, _>, _>>()
            .map(Self)
    }
}

pub(crate) struct PreparedRule {
    pub(crate) config: AdmissionConfig,
    pub(crate) name: &'static str,
    pub(crate) rule: Arc<dyn AdmissionRule>,
}

#[derive(Default)]
struct RegistryState {
    current: BTreeMap<String, Arc<Admission>>,
    retired: BTreeMap<String, Weak<Admission>>,
    closed: bool,
}

/// Long-lived model rule directory, shared by the runtime publisher and request execution.
#[derive(Default)]
pub struct AdmissionRegistry(Mutex<RegistryState>);

impl AdmissionRegistry {
    /// Commits a validated model directory at the runtime's publication boundary.
    /// Built-in rules update their shared capacity in place; other changed rules drain independently.
    pub fn publish(&self, prepared: PreparedAdmissions) {
        let mut registry = self.0.lock().expect("admission registry lock poisoned");
        if registry.closed {
            return;
        }
        let mut previous = std::mem::take(&mut registry.current);
        let mut current = BTreeMap::new();
        for (model, rule) in prepared.0 {
            let entry = previous
                .remove(&model)
                .or_else(|| {
                    registry
                        .retired
                        .remove(&model)
                        .and_then(|entry| entry.upgrade())
                })
                .unwrap_or_else(|| Arc::new(Admission::new(model.clone())));
            entry.replace(Some(rule));
            current.insert(model, entry);
        }
        for (model, entry) in previous {
            entry.replace(None);
            registry.retired.insert(model, Arc::downgrade(&entry));
        }
        registry.retired.retain(|_, entry| entry.strong_count() > 0);
        set_configured_models(&current.keys().cloned().collect::<Vec<_>>());
        registry.current = current;
    }

    /// Reports whether every currently configured model has activated its published rule.
    /// Runtime status uses this to defer acknowledgement while a custom rule is still draining.
    pub fn is_applied(&self) -> bool {
        let registry = self.0.lock().expect("admission registry lock poisoned");
        !registry.closed
            && registry.current.values().all(|entry| {
                let state = entry.state.lock().expect("model admission lock poisoned");
                !state.draining && state.active.is_some() && state.pending.is_none()
            })
    }

    /// Returns the currently configured model handle; arbitrary request names create no state.
    pub fn model(&self, model: &str) -> Option<Arc<Admission>> {
        let registry = self.0.lock().expect("admission registry lock poisoned");
        if registry.closed {
            return None;
        }
        registry.current.get(model).cloned()
    }

    /// Cancels waiters and rejects new work while existing permits retain their resources.
    pub fn close(&self) {
        let mut registry = self.0.lock().expect("admission registry lock poisoned");
        registry.closed = true;
        for entry in registry.current.values() {
            entry.replace(None);
        }
    }
}

struct ActiveRule {
    prepared: PreparedRule,
    metrics: AdmissionMetricsScope,
}

#[derive(Default)]
struct ModelState {
    active: Option<ActiveRule>,
    pending: Option<PreparedRule>,
    events: Option<AdmissionModelMetrics>,
    draining: bool,
    users: usize,
}

/// One model's stable admission handle, retained across routing generations and rule changes.
pub struct Admission {
    model: String,
    state: Mutex<ModelState>,
    changes: tokio::sync::watch::Sender<u64>,
}

impl Admission {
    fn new(model: String) -> Self {
        let (changes, _) = tokio::sync::watch::channel(0);
        Self {
            model,
            state: Mutex::new(ModelState::default()),
            changes,
        }
    }

    /// Reports whether admission needs a prepared model; handover decisions remain in admit.
    pub fn requires_ready_runtime(&self) -> bool {
        let state = self.state.lock().expect("model admission lock poisoned");
        !state.draining
            && state
                .active
                .as_ref()
                .is_some_and(|active| active.prepared.rule.requires_ready_runtime())
    }

    // Publication preserves the built-in capacity owner; custom changes and removal close the
    // old rule and wait for all attempt and accepted-batch leases before replacing its lifecycle.
    fn replace(&self, mut replacement: Option<PreparedRule>) {
        let mut state = self.state.lock().expect("model admission lock poisoned");
        if !state.draining
            && state
                .active
                .as_ref()
                .zip(replacement.as_ref())
                .is_some_and(|(active, next)| active.prepared.config == next.config)
        {
            return;
        }
        if !state.draining
            && let Some(active) = state.active.as_mut()
            && let Some(next) = replacement.as_mut()
            && let Some((owner, candidate)) = active
                .prepared
                .rule
                .capacity_state()
                .zip(next.rule.capacity_state())
        {
            owner.update(candidate);
            active
                .metrics
                .update(next.name, active.prepared.rule.capacity());
            next.rule = active.prepared.rule.clone();
            std::mem::swap(&mut active.prepared, next);
            return;
        }
        state.pending = replacement;
        if !state.draining {
            state.draining = true;
            if let Some(active) = &state.active {
                active.metrics.set_draining(true);
                active.prepared.rule.close();
            }
            self.changes
                .send_modify(|version| *version = version.wrapping_add(1));
        }
        self.activate_if_drained(&mut state);
    }

    fn activate_if_drained(&self, state: &mut ModelState) {
        if !state.draining || state.users != 0 {
            return;
        }
        // Drop retired metadata before registering the successor under the same model labels.
        state.active = None;
        if let Some(prepared) = state.pending.take() {
            state
                .events
                .get_or_insert_with(|| AdmissionModelMetrics::new(&self.model));
            let metrics =
                AdmissionMetricsScope::new(&self.model, prepared.name, prepared.rule.capacity());
            state.active = Some(ActiveRule { prepared, metrics });
            state.draining = false;
        } else {
            // Retire events before the last strong reference can disappear; a concurrent
            // re-add either reuses this locked entry or starts after its series are gone.
            state.events = None;
        }
    }

    /// Executes one atomic model admission and transfers its lifecycle lease to the returned permit.
    pub async fn admit(
        self: &Arc<Self>,
        request: &AdmissionRequest,
        deadline: tokio::time::Instant,
        service: AdmissionService,
        reader: &dyn AdmissionStateReader,
    ) -> Result<AdmissionPermit, AdmissionError> {
        let mut changes = self.changes.subscribe();
        let (rule, metrics, lease, attempt) = {
            let mut state = self.state.lock().expect("model admission lock poisoned");
            let Some(active) = &state.active else {
                return Err(AdmissionError::Closed);
            };
            let attempt = AdmissionAttempt::work(&self.model, deadline);
            if state.draining {
                let result: Result<AdmissionPermit, _> = Err(AdmissionError::Closed);
                attempt.complete(&result);
                return result;
            }
            let rule = active.prepared.rule.clone();
            let metrics = active.metrics.metrics();
            state.users += 1;
            // Refresh the watch version under the same lock as accepting this attempt.
            changes.borrow_and_update();
            (rule, metrics, WorkLease(self.clone()), attempt)
        };
        let context = AdmissionContext {
            deadline,
            service,
            state: reader,
            queue: attempt.queue(),
            metrics,
        };
        let result = attempt
            .run(async {
                let permit = tokio::select! {
                    biased;
                    _ = changes.changed() => return Err(AdmissionError::Closed),
                    result = rule.admit(request, &context) => result?,
                };
                let state = self.state.lock().expect("model admission lock poisoned");
                if state.draining {
                    return Err(AdmissionError::Closed);
                }
                Ok(permit)
            })
            .await;
        // Attempt records finish before the last lease can retire this model's metric scope.
        result.map(|permit| {
            AdmissionPermit::new(WorkReservation {
                permit,
                lease: Arc::new(lease),
            })
        })
    }
}

struct WorkLease(Arc<Admission>);
impl Drop for WorkLease {
    fn drop(&mut self) {
        let mut state = self.0.state.lock().expect("model admission lock poisoned");
        state.users -= 1;
        self.0.activate_if_drained(&mut state);
    }
}

// The resource permit is dropped before its lifecycle lease. Splits share a batch lease so
// a draining custom rule cannot be replaced while any child or undistributed unit is alive.
struct WorkReservation {
    permit: AdmissionPermit,
    lease: Arc<WorkLease>,
}
impl AdmissionReservation for WorkReservation {
    fn split_one(&mut self) -> Box<dyn AdmissionReservation> {
        Box::new(Self {
            permit: self.permit.split_one(),
            lease: self.lease.clone(),
        })
    }
}
