// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Candidate ownership through preparation, dispatch, acceptance, and cancellation.

use std::ops::Range;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use foretoken_request_ledger::{Outcome, RequestLedger, ReservationRef};
use tokio::time::Instant;

use crate::telemetry::AdmissionAttempt;
use crate::{Admission, AdmissionError};

/// Role constraints captured when the complete request receives a dispatch grant.
#[derive(Clone, Debug, Default)]
pub struct DispatchRules {
    pub priority: Option<i32>,
    pub allowed_pools: Vec<String>,
}

#[derive(Default)]
struct DispatchState {
    rules: Option<DispatchRules>,
    accepted: bool,
}

struct SharedReservation {
    ledger: Arc<RequestLedger>,
    reference: ReservationRef,
}

pub(crate) struct BatchReservation {
    model: Arc<Admission>,
    shared: Option<SharedReservation>,
    expires: Instant,
    deadline: Instant,
    dispatch: tokio::sync::Mutex<DispatchState>,
    observation: Mutex<Option<AdmissionAttempt>>,
}

impl BatchReservation {
    pub(crate) fn new(
        model: Arc<Admission>,
        ledger: Arc<RequestLedger>,
        reference: ReservationRef,
        expires: Instant,
        deadline: Instant,
    ) -> Self {
        Self {
            model,
            shared: Some(SharedReservation { ledger, reference }),
            expires,
            deadline,
            dispatch: tokio::sync::Mutex::new(DispatchState::default()),
            observation: Mutex::new(None),
        }
    }

    fn timeout_error(&self) -> AdmissionError {
        if Instant::now() >= self.deadline {
            AdmissionError::DeadlineExceeded
        } else {
            AdmissionError::QueueTimeout
        }
    }

    // Children share one grant, transferring reserved units rather than rejoining the queue.
    async fn dispatch(&self) -> Result<DispatchRules, AdmissionError> {
        let mut state = self.dispatch.lock().await;
        if Instant::now() >= self.expires {
            return Err(self.timeout_error());
        }
        if let Some(rules) = &state.rules {
            return Ok(rules.clone());
        }
        let Some(shared) = &self.shared else {
            self.model.check_unrestricted()?;
            let rules = DispatchRules::default();
            state.rules = Some(rules.clone());
            return Ok(rules);
        };
        self.model.ready(&shared.reference.request)?;
        loop {
            let changed = self.model.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            if Instant::now() >= self.expires {
                return Err(self.timeout_error());
            }
            tokio::time::timeout_at(self.expires, self.model.grant_ready())
                .await
                .map_err(|_| self.timeout_error())??;
            if Instant::now() >= self.expires {
                return Err(self.timeout_error());
            }
            if self.model.granted(&shared.reference.request)? {
                let rules = self.model.dispatch_rules(&shared.reference.request)?;
                state.rules = Some(rules.clone());
                return Ok(rules);
            }
            tokio::select! {
                _ = changed => {},
                _ = tokio::time::sleep_until(self.expires) => return Err(self.timeout_error()),
                // Other replicas release shared capacity without a local Notify sender.
                _ = tokio::time::sleep(Duration::from_millis(100)) => {},
            }
        }
    }
}

impl Drop for BatchReservation {
    fn drop(&mut self) {
        if let Some(shared) = &self.shared {
            self.model.remove(&shared.reference.request);
        }
    }
}

/// Transferable ownership of candidate slots. Accepted slots remain charged after consumer drop;
/// only the model-server's engine-completion path can release their execution capacity.
#[derive(Default)]
pub struct AdmissionPermit {
    batch: Option<Arc<BatchReservation>>,
    slots: Range<u32>,
}

impl AdmissionPermit {
    pub(crate) fn unrestricted(
        model: Arc<Admission>,
        expires: Instant,
        deadline: Instant,
        units: u32,
    ) -> Self {
        Self {
            batch: Some(Arc::new(BatchReservation {
                model,
                shared: None,
                expires,
                deadline,
                dispatch: tokio::sync::Mutex::new(DispatchState::default()),
                observation: Mutex::new(None),
            })),
            slots: 0..units,
        }
    }

    pub(crate) fn batch(batch: Arc<BatchReservation>, units: u32) -> Self {
        Self {
            batch: Some(batch),
            slots: 0..units,
        }
    }

    pub(crate) fn observe(&self, observation: AdmissionAttempt) {
        if let Some(batch) = &self.batch {
            *batch
                .observation
                .lock()
                .expect("admission observation lock poisoned") = Some(observation);
        }
    }

    /// Clones observation without transferring capacity, for preparation and execution error reporting.
    pub fn observation(&self) -> Option<AdmissionAttempt> {
        self.batch.as_ref().and_then(|batch| {
            batch
                .observation
                .lock()
                .expect("admission observation lock poisoned")
                .clone()
        })
    }

    /// Reports whether non-cancelable preparation must retain shared waiting ownership.
    pub fn is_reserved(&self) -> bool {
        self.batch
            .as_ref()
            .is_some_and(|batch| batch.shared.is_some())
    }

    /// Returns the original budget covering readiness, preparation, and initial submission.
    pub fn waiting_deadline(&self) -> Option<Instant> {
        self.batch.as_ref().map(|batch| batch.expires)
    }

    /// Transfers one candidate's existing ownership without acquiring capacity again.
    pub fn split_one(&mut self) -> Self {
        if self.batch.is_none() {
            return Self::default();
        }
        let slot = self
            .slots
            .next()
            .expect("batch child has an unassigned reservation");
        Self {
            batch: self.batch.clone(),
            slots: slot..slot + 1,
        }
    }

    /// Reads role constraints for readiness observations without reserving execution capacity.
    pub async fn waiting_rules(&self) -> Result<DispatchRules, AdmissionError> {
        let Some(batch) = &self.batch else {
            return Ok(DispatchRules::default());
        };
        let state = batch.dispatch.lock().await;
        if let Some(rules) = &state.rules {
            return Ok(rules.clone());
        }
        match &batch.shared {
            Some(shared) => batch.model.dispatch_rules(&shared.reference.request),
            None => {
                batch.model.check_unrestricted()?;
                Ok(DispatchRules::default())
            }
        }
    }

    /// Waits for priority/caller selection and atomically reserves the complete batch's concurrency.
    pub async fn dispatch(&self) -> Result<DispatchRules, AdmissionError> {
        let result = match &self.batch {
            Some(batch) => batch.dispatch().await,
            None => Ok(DispatchRules::default()),
        };
        if result.is_err()
            && let Some(observation) = self.observation()
        {
            observation.complete(&result);
        }
        result
    }

    /// Supplies the internal capacity reference submitted with one candidate to model-server.
    pub fn reference(&self) -> Option<ReservationRef> {
        self.batch
            .as_ref()
            .and_then(|batch| batch.shared.as_ref())
            .map(|shared| ReservationRef {
                slot: self.slots.start,
                ..shared.reference.clone()
            })
    }

    /// Advances local rotation after explicit backend acceptance, leaving capacity with the engine owner.
    pub async fn accepted(&self) {
        if let Some(batch) = &self.batch {
            batch.dispatch.lock().await.accepted = true;
            if let Some(shared) = &batch.shared {
                batch.model.accepted(&shared.reference.request);
            }
        }
        if let Some(observation) = self.observation() {
            observation.complete(&Ok::<(), AdmissionError>(()));
        }
    }

    /// Returns a definitely unaccepted batch to its original queue position.
    /// A batch with an accepted child must finish or cancel instead of replaying initial submission.
    pub async fn retry(&self) -> Result<bool, AdmissionError> {
        let Some(batch) = &self.batch else {
            return Ok(false);
        };
        let Some(shared) = &batch.shared else {
            return Ok(false);
        };
        let mut state = batch.dispatch.lock().await;
        if state.accepted {
            return Ok(false);
        }
        match shared.ledger.retry(&shared.reference).await? {
            Outcome::Applied => {
                state.rules = None;
                batch.model.defer(&shared.reference.request);
                Ok(true)
            }
            Outcome::AlreadyAccepted => Ok(false),
            _ => Err(AdmissionError::Closed),
        }
    }
}

impl Drop for AdmissionPermit {
    fn drop(&mut self) {
        let Some(batch) = self.batch.clone() else {
            return;
        };
        let Some(shared) = &batch.shared else {
            return;
        };
        let slots = self.slots.clone();
        if slots.is_empty() {
            return;
        }
        batch.model.remove(&shared.reference.request);
        // Cleanup retains ownership across transient storage failure. Cancel marks accepted slots;
        // it never turns a disconnected output consumer into evidence of engine termination.
        tokio::spawn(async move {
            let shared = batch
                .shared
                .as_ref()
                .expect("shared reservation was retained");
            for slot in slots {
                let reference = ReservationRef {
                    slot,
                    ..shared.reference.clone()
                };
                loop {
                    match shared.ledger.cancel(&reference).await {
                        Ok(()) => break,
                        Err(error) => {
                            tracing::warn!(error = %error, "request reservation cleanup will retry");
                            tokio::time::sleep(Duration::from_secs(1)).await;
                        }
                    }
                }
            }
            batch.model.changed.notify_waiters();
        });
    }
}
