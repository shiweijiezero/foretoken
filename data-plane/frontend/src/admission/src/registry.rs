// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Stable per-model waiting owners and local priority/caller dispatch order.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex, Weak};
use std::time::Duration;

use foretoken_request_ledger::{Outcome, RequestLedger, ReservationRef};
use tokio::sync::Notify;
use tokio::time::Instant;

use crate::permit::BatchReservation;
use crate::{
    AdmissionConfig, AdmissionConfigError, AdmissionError, AdmissionPermit, AdmissionRequest,
};

/// Validated settings prepared without changing active queues or shared capacity.
pub struct PreparedAdmissions {
    configs: BTreeMap<String, AdmissionConfig>,
    members: Arc<AdmissionMembers>,
}

#[derive(Default)]
struct AdmissionMembers {
    frontends: Vec<String>,
    backends: Vec<String>,
}

impl PreparedAdmissions {
    /// Validates every model before the runtime commits the serving publication.
    pub fn new(
        configs: &BTreeMap<String, AdmissionConfig>,
        frontend_instances: &[String],
        backend_instances: &[String],
    ) -> Result<Self, AdmissionConfigError> {
        for config in configs.values() {
            config.validate()?;
        }
        Ok(Self {
            configs: configs.clone(),
            members: Arc::new(AdmissionMembers {
                frontends: frontend_instances.to_vec(),
                backends: backend_instances.to_vec(),
            }),
        })
    }
}

#[derive(Default)]
struct RegistryState {
    current: BTreeMap<String, Arc<Admission>>,
    retired: BTreeMap<String, Weak<Admission>>,
    closed: bool,
}

/// Owns model queues for one frontend process; the ledger owns cross-replica capacity.
pub struct AdmissionRegistry {
    state: Mutex<RegistryState>,
    scope: String,
    frontend: String,
    ledger: Option<Arc<RequestLedger>>,
}

impl AdmissionRegistry {
    /// Creates a registry using controller-provided service identity and the platform store.
    pub fn new(scope: String, frontend: String, ledger: Option<Arc<RequestLedger>>) -> Self {
        Self {
            state: Mutex::new(RegistryState::default()),
            scope,
            frontend,
            ledger,
        }
    }

    /// Activates a validated publication while preserving queues and outstanding reservations.
    pub fn publish(&self, version: u64, prepared: PreparedAdmissions) {
        let mut state = self.state.lock().expect("admission registry lock poisoned");
        if state.closed {
            return;
        }
        let mut previous = std::mem::take(&mut state.current);
        for (model, config) in prepared.configs {
            let entry = previous
                .remove(&model)
                .or_else(|| {
                    state
                        .retired
                        .remove(&model)
                        .and_then(|entry| entry.upgrade())
                })
                .unwrap_or_else(|| {
                    Arc::new(Admission::new(
                        model.clone(),
                        self.scope.clone(),
                        self.frontend.clone(),
                        self.ledger.clone(),
                    ))
                });
            entry.replace(version, Some(config), prepared.members.clone());
            state.current.insert(model, entry);
        }
        for (model, entry) in previous {
            entry.replace(version, None, prepared.members.clone());
            state.retired.insert(model, Arc::downgrade(&entry));
        }
        state.retired.retain(|_, entry| entry.strong_count() > 0);
        crate::telemetry::set_configured_models(&state.current.keys().cloned().collect::<Vec<_>>());
        for (model, entry) in &state.current {
            if let Some(config) = &entry
                .state
                .lock()
                .expect("model admission lock poisoned")
                .config
            {
                crate::telemetry::set_model_config(model, &self.scope, config);
            }
        }
    }

    /// Looks up a configured model without allocating state for arbitrary request names.
    pub fn model(&self, model: &str) -> Option<Arc<Admission>> {
        let state = self.state.lock().expect("admission registry lock poisoned");
        if state.closed {
            None
        } else {
            state.current.get(model).cloned()
        }
    }

    /// Reports whether all current settings have reached the shared reservation authority.
    pub fn is_applied(&self) -> bool {
        let state = self.state.lock().expect("admission registry lock poisoned");
        !state.closed
            && state.current.values().all(|entry| {
                let model = entry.state.lock().expect("model admission lock poisoned");
                model.applied_version == Some(model.version)
            })
    }

    /// Samples authoritative capacity for metrics without feeding observations into dispatch.
    pub async fn refresh_observations(&self) {
        let models: Vec<_> = self
            .state
            .lock()
            .expect("admission registry lock poisoned")
            .current
            .values()
            .cloned()
            .collect();
        for model in models {
            let Ok((_, config, _)) = model.configuration() else {
                continue;
            };
            if self.ledger.is_none() && config.max_waiting_requests.is_none() {
                continue;
            }
            let occupancy = match &self.ledger {
                Some(ledger) => ledger.occupancy(&self.scope, &model.model).await.ok(),
                None => None,
            };
            crate::telemetry::observe_ledger(&model.model, &self.scope, occupancy);
        }
    }

    /// Wakes pending requests on shutdown; accepted work remains owned by its execution lifecycle.
    pub fn close(&self) {
        let mut state = self.state.lock().expect("admission registry lock poisoned");
        state.closed = true;
        for entry in state.current.values() {
            entry
                .state
                .lock()
                .expect("model admission lock poisoned")
                .config = None;
            entry.changed.notify_waiters();
        }
    }
}

#[derive(Clone)]
struct WaitingRequest {
    reservation: ReservationRef,
    caller: String,
    role: Option<String>,
    ready: bool,
    reserved: bool,
    error: Option<AdmissionError>,
    retry_at: Instant,
}

#[derive(Default)]
struct ModelState {
    version: u64,
    config: Option<AdmissionConfig>,
    members: Arc<AdmissionMembers>,
    applied_version: Option<u64>,
    waiting: Vec<WaitingRequest>,
    last_caller: BTreeMap<i32, String>,
}

/// A model's local request ordering, retained across routing and configuration generations.
pub struct Admission {
    model: String,
    scope: String,
    frontend: String,
    ledger: Option<Arc<RequestLedger>>,
    state: Mutex<ModelState>,
    dispatch: tokio::sync::Mutex<()>,
    owner_epoch: tokio::sync::OnceCell<u64>,
    pub(crate) changed: Notify,
}

impl Admission {
    fn new(
        model: String,
        scope: String,
        frontend: String,
        ledger: Option<Arc<RequestLedger>>,
    ) -> Self {
        Self {
            model,
            scope,
            frontend,
            ledger,
            state: Mutex::new(ModelState::default()),
            dispatch: tokio::sync::Mutex::new(()),
            owner_epoch: tokio::sync::OnceCell::new(),
            changed: Notify::new(),
        }
    }

    fn replace(
        self: &Arc<Self>,
        version: u64,
        config: Option<AdmissionConfig>,
        members: Arc<AdmissionMembers>,
    ) {
        let mut state = self.state.lock().expect("model admission lock poisoned");
        state.version = version;
        state.members = members.clone();
        let tracked = config
            .as_ref()
            .is_some_and(|config| config.max_waiting_requests.is_some());
        state.applied_version = (!tracked).then_some(version);
        state.config = config.clone();
        drop(state);
        self.changed.notify_waiters();
        let Some(ledger) = self.ledger.clone() else {
            return;
        };
        let scope = self.scope.clone();
        let model = self.model.clone();
        let owner = Arc::downgrade(self);
        let config = config.map_or_else(
            || serde_json::json!({"closed": true}),
            |config| serde_json::to_value(config).expect("admission settings serialize"),
        );
        // Publication is not coupled to new traffic. Monotonic store revisions also make delayed
        // retries harmless when another replica has already published a newer configuration.
        tokio::spawn(async move {
            loop {
                if owner.upgrade().is_some_and(|owner| {
                    owner
                        .state
                        .lock()
                        .expect("model admission lock poisoned")
                        .version
                        != version
                }) {
                    return;
                }
                if ledger
                    .publish(
                        &scope,
                        &model,
                        version,
                        config.clone(),
                        &members.frontends,
                        &members.backends,
                    )
                    .await
                    .is_ok()
                {
                    if let Some(owner) = owner.upgrade() {
                        if tracked && owner.initialize_owner().await.is_err() {
                            tokio::time::sleep(Duration::from_secs(1)).await;
                            continue;
                        }
                        let mut state = owner.state.lock().expect("model admission lock poisoned");
                        if state.version == version {
                            state.applied_version = Some(version);
                        }
                    }
                    return;
                }
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        });
    }

    fn configuration(
        &self,
    ) -> Result<(u64, AdmissionConfig, Arc<AdmissionMembers>), AdmissionError> {
        let state = self.state.lock().expect("model admission lock poisoned");
        Ok((
            state.version,
            state.config.clone().ok_or(AdmissionError::Closed)?,
            state.members.clone(),
        ))
    }

    async fn initialize_owner(&self) -> Result<u64, AdmissionError> {
        let ledger = self
            .ledger
            .as_ref()
            .ok_or(AdmissionError::StoreUnavailable)?;
        self.owner_epoch
            .get_or_try_init(|| async {
                ledger
                    .register_frontend(&self.scope, &self.model, &self.frontend)
                    .await
                    .map_err(AdmissionError::from)
            })
            .await
            .copied()
    }

    pub(crate) fn check_unrestricted(&self) -> Result<(), AdmissionError> {
        let state = self.state.lock().expect("model admission lock poisoned");
        let config = state.config.as_ref().ok_or(AdmissionError::Closed)?;
        if config.max_waiting_requests.is_some() || !config.role_rules.is_empty() {
            return Err(AdmissionError::Closed);
        }
        Ok(())
    }

    /// Reserves waiting capacity before preprocessing, without occupying execution concurrency.
    pub async fn enter(
        self: &Arc<Self>,
        request: &AdmissionRequest,
        deadline: Instant,
    ) -> Result<AdmissionPermit, AdmissionError> {
        let observation = crate::telemetry::AdmissionAttempt::work(&self.model, deadline);
        let resolved = match self.configuration() {
            Ok(resolved) => resolved,
            Err(error) => {
                observation.complete(&Err::<(), _>(error));
                return Err(error);
            }
        };
        let expires = resolved
            .1
            .wait_timeout()
            .expect("published admission timeout is valid")
            .and_then(|timeout| Instant::now().checked_add(timeout))
            .map_or(deadline, |waiting_deadline| waiting_deadline.min(deadline));
        let model = self.clone();
        let request = request.clone();
        // A canceled caller cannot abandon a store command whose result is still unknown. The
        // detached task finishes enqueue and drops any unclaimed permit, without dispatching work.
        let entry = tokio::spawn(async move {
            model
                .enter_waiting(&request, expires, deadline, resolved)
                .await
        });
        let result = match tokio::time::timeout_at(expires, entry).await {
            Ok(result) => result.expect("admission entry task must not panic"),
            Err(_) if expires == deadline => Err(AdmissionError::DeadlineExceeded),
            Err(_) => Err(AdmissionError::QueueTimeout),
        };
        let result = if Instant::now() >= expires {
            drop(result);
            Err(if expires == deadline {
                AdmissionError::DeadlineExceeded
            } else {
                AdmissionError::QueueTimeout
            })
        } else {
            result
        };
        match result {
            Ok(permit) => {
                if permit.is_reserved() {
                    observation.begin_wait();
                }
                permit.observe(observation);
                Ok(permit)
            }
            Err(error) => {
                observation.complete(&Err::<(), _>(error));
                Err(error)
            }
        }
    }

    async fn enter_waiting(
        self: &Arc<Self>,
        request: &AdmissionRequest,
        expires: Instant,
        deadline: Instant,
        resolved: (u64, AdmissionConfig, Arc<AdmissionMembers>),
    ) -> Result<AdmissionPermit, AdmissionError> {
        let (version, config, members) = resolved;
        if request.units == 0 {
            return Err(AdmissionError::BatchTooLarge);
        }
        let role = config
            .role_rules
            .iter()
            .find(|rule| request.caller.role.as_deref() == Some(rule.role.as_str()));
        if !config.role_rules.is_empty()
            && (role.is_none() || request.caller.caller.as_deref().is_none_or(str::is_empty))
        {
            return Err(AdmissionError::IdentityRequired);
        }
        if Instant::now() >= expires {
            return Err(AdmissionError::QueueTimeout);
        }
        if config.max_waiting_requests.is_none() && config.role_rules.is_empty() {
            return Ok(AdmissionPermit::unrestricted(
                self.clone(),
                expires,
                deadline,
                request.units,
            ));
        }
        let ledger = self
            .ledger
            .clone()
            .ok_or(AdmissionError::StoreUnavailable)?;
        let reservation = ReservationRef {
            scope: self.scope.clone(),
            model: self.model.clone(),
            request: uuid::Uuid::new_v4().to_string(),
            slot: 0,
            final_stage: true,
        };
        let caller = request.caller.caller.clone().unwrap_or_default();
        ledger
            .publish(
                &self.scope,
                &self.model,
                version,
                serde_json::to_value(&config).expect("admission settings serialize"),
                &members.frontends,
                &members.backends,
            )
            .await?;
        let epoch = self.initialize_owner().await?;
        let outcome = ledger
            .enqueue(
                &reservation,
                &self.frontend,
                epoch,
                &caller,
                request.caller.role.as_deref().unwrap_or_default(),
                request.units,
            )
            .await;
        let outcome = match outcome {
            Ok(outcome) => outcome,
            Err(error) => {
                let frontend = self.frontend.clone();
                tokio::spawn(async move {
                    while ledger
                        .cancel_entry(&reservation, &frontend, epoch, &caller)
                        .await
                        .is_err()
                    {
                        tokio::time::sleep(Duration::from_secs(1)).await;
                    }
                });
                return Err(error.into());
            }
        };
        match outcome {
            Outcome::Applied => {}
            Outcome::Full => return Err(AdmissionError::Overloaded),
            Outcome::BatchTooLarge => return Err(AdmissionError::BatchTooLarge),
            Outcome::InvalidRole => return Err(AdmissionError::IdentityRequired),
            _ => return Err(AdmissionError::Closed),
        }
        let batch = Arc::new(BatchReservation::new(
            self.clone(),
            ledger,
            reservation.clone(),
            expires,
            deadline,
        ));
        let permit = AdmissionPermit::batch(batch, request.units);
        self.configuration()?;
        self.state
            .lock()
            .expect("model admission lock poisoned")
            .waiting
            .push(WaitingRequest {
                reservation,
                caller,
                role: request.caller.role.clone(),
                ready: false,
                reserved: false,
                error: None,
                retry_at: Instant::now(),
            });
        self.changed.notify_waiters();
        Ok(permit)
    }

    pub(crate) fn ready(&self, id: &str) -> Result<(), AdmissionError> {
        let mut state = self.state.lock().expect("model admission lock poisoned");
        if state.config.is_none() {
            return Err(AdmissionError::Closed);
        }
        if let Some(entry) = state
            .waiting
            .iter_mut()
            .find(|entry| entry.reservation.request == id)
        {
            entry.ready = true;
        }
        drop(state);
        self.changed.notify_waiters();
        Ok(())
    }

    pub(crate) fn granted(&self, id: &str) -> Result<bool, AdmissionError> {
        let state = self.state.lock().expect("model admission lock poisoned");
        let config = state.config.as_ref().ok_or(AdmissionError::Closed)?;
        let Some(entry) = state
            .waiting
            .iter()
            .find(|entry| entry.reservation.request == id)
        else {
            return Err(AdmissionError::Closed);
        };
        if let Some(error) = entry.error {
            return Err(error);
        }
        if !config.role_rules.is_empty()
            && !config
                .role_rules
                .iter()
                .any(|rule| entry.role.as_deref() == Some(&rule.role))
        {
            return Err(AdmissionError::IdentityRequired);
        }
        Ok(entry.reserved)
    }

    // Only caller heads are considered. Preparing or deferred heads preserve that caller's FIFO
    // without blocking other callers; priority bands are visited from highest to lowest.
    pub(crate) async fn grant_ready(&self) -> Result<(), AdmissionError> {
        let _dispatch = self.dispatch.lock().await;
        let (version, config, members) = self.configuration()?;
        let ledger = self
            .ledger
            .as_ref()
            .ok_or(AdmissionError::StoreUnavailable)?;
        ledger
            .publish(
                &self.scope,
                &self.model,
                version,
                serde_json::to_value(config).expect("admission settings serialize"),
                &members.frontends,
                &members.backends,
            )
            .await?;
        let candidates = {
            let state = self.state.lock().expect("model admission lock poisoned");
            let config = state.config.as_ref().ok_or(AdmissionError::Closed)?;
            let mut callers = BTreeSet::new();
            let mut bands: BTreeMap<i32, Vec<WaitingRequest>> = BTreeMap::new();
            for entry in &state.waiting {
                if !callers.insert(entry.caller.clone()) || entry.reserved {
                    continue;
                }
                if entry.error.is_some() || !entry.ready || entry.retry_at > Instant::now() {
                    continue;
                }
                let priority = if config.role_rules.is_empty() {
                    0
                } else {
                    let Some(rule) = config
                        .role_rules
                        .iter()
                        .find(|rule| entry.role.as_deref() == Some(&rule.role))
                    else {
                        continue;
                    };
                    rule.priority
                };
                bands.entry(priority).or_default().push(entry.clone());
            }
            let mut candidates = Vec::new();
            for (priority, mut entries) in bands.into_iter().rev() {
                entries.sort_by(|a, b| a.caller.cmp(&b.caller));
                if let Some(last) = state.last_caller.get(&priority) {
                    let offset = entries.partition_point(|entry| &entry.caller <= last);
                    entries.rotate_left(offset);
                }
                candidates.extend(entries);
            }
            candidates
        };
        for candidate in candidates {
            match ledger.dispatch(&candidate.reservation).await? {
                Outcome::Applied => {
                    if let Some(entry) = self
                        .state
                        .lock()
                        .expect("model admission lock poisoned")
                        .waiting
                        .iter_mut()
                        .find(|entry| entry.reservation.request == candidate.reservation.request)
                    {
                        entry.reserved = true;
                    }
                    self.changed.notify_waiters();
                    return Ok(());
                }
                Outcome::Busy => continue,
                result => {
                    let error = match result {
                        Outcome::BatchTooLarge => AdmissionError::BatchTooLarge,
                        Outcome::InvalidRole => AdmissionError::IdentityRequired,
                        _ => AdmissionError::Closed,
                    };
                    if let Some(entry) = self
                        .state
                        .lock()
                        .expect("model admission lock poisoned")
                        .waiting
                        .iter_mut()
                        .find(|entry| entry.reservation.request == candidate.reservation.request)
                    {
                        entry.error = Some(error);
                    }
                    self.changed.notify_waiters();
                }
            }
        }
        Ok(())
    }

    pub(crate) fn dispatch_rules(&self, id: &str) -> Result<crate::DispatchRules, AdmissionError> {
        let state = self.state.lock().expect("model admission lock poisoned");
        let config = state.config.as_ref().ok_or(AdmissionError::Closed)?;
        if config.role_rules.is_empty() {
            return Ok(crate::DispatchRules::default());
        }
        let entry = state
            .waiting
            .iter()
            .find(|entry| entry.reservation.request == id)
            .ok_or(AdmissionError::Closed)?;
        let rule = config
            .role_rules
            .iter()
            .find(|rule| entry.role.as_deref() == Some(&rule.role))
            .ok_or(AdmissionError::IdentityRequired)?;
        Ok(crate::DispatchRules {
            priority: Some(rule.priority),
            allowed_pools: rule.allowed_pools.clone(),
        })
    }

    pub(crate) fn accepted(&self, id: &str) {
        let mut state = self.state.lock().expect("model admission lock poisoned");
        if let Some(index) = state
            .waiting
            .iter()
            .position(|entry| entry.reservation.request == id)
        {
            let entry = state.waiting.remove(index);
            let priority = state
                .config
                .as_ref()
                .and_then(|config| {
                    config
                        .role_rules
                        .iter()
                        .find(|rule| entry.role.as_deref() == Some(&rule.role))
                })
                .map_or(0, |rule| rule.priority);
            state.last_caller.insert(priority, entry.caller);
        }
        drop(state);
        self.changed.notify_waiters();
    }

    pub(crate) fn defer(&self, id: &str) {
        if let Some(entry) = self
            .state
            .lock()
            .expect("model admission lock poisoned")
            .waiting
            .iter_mut()
            .find(|entry| entry.reservation.request == id)
        {
            entry.reserved = false;
            entry.ready = false;
            entry.retry_at = Instant::now() + Duration::from_millis(100);
        }
        self.changed.notify_waiters();
    }

    pub(crate) fn remove(&self, id: &str) {
        self.state
            .lock()
            .expect("model admission lock poisoned")
            .waiting
            .retain(|entry| entry.reservation.request != id);
        self.changed.notify_waiters();
    }
}
