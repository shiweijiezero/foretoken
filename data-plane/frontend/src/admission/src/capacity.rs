// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Stable work-unit accounting and FIFO waiting for the built-in admission rules.

use std::collections::VecDeque;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use prometheus_client::metrics::gauge::Gauge;
use tokio::sync::watch;

use crate::{
    AdmissionCapacity, AdmissionContext, AdmissionError, AdmissionPermit, AdmissionRequest,
    AdmissionReservation,
};

struct CapacityState {
    limits: Option<AdmissionCapacity>,
    queue_timeout: Option<Duration>,
    active: u64,
    queued: u64,
    waiters: VecDeque<Arc<()>>,
    closed: bool,
}

/// Shared capacity owner retained when a built-in rule changes its limits or algorithm.
/// Running reservations and waiting futures retain this owner, not a retired configuration.
#[derive(Clone)]
pub struct AdmissionCapacityState {
    state: Arc<Mutex<CapacityState>>,
    changes: watch::Sender<()>,
}

impl AdmissionCapacityState {
    /// Creates an independent owner for a validated, unpublished built-in rule.
    pub(crate) fn new(limits: Option<AdmissionCapacity>, queue_timeout: Option<Duration>) -> Self {
        let (changes, _) = watch::channel(());
        Self {
            state: Arc::new(Mutex::new(CapacityState {
                limits,
                queue_timeout,
                active: 0,
                queued: 0,
                waiters: VecDeque::new(),
                closed: false,
            })),
            changes,
        }
    }

    pub(crate) fn capacity(&self) -> Option<AdmissionCapacity> {
        self.state
            .lock()
            .expect("admission capacity lock poisoned")
            .limits
    }

    /// Commits the candidate's settings without replacing counters, waiters, or their budgets.
    /// The registry calls this only while publishing a still-active model owner.
    pub(crate) fn update(&self, candidate: &Self) {
        let (limits, queue_timeout) = {
            let candidate = candidate
                .state
                .lock()
                .expect("admission capacity lock poisoned");
            (candidate.limits, candidate.queue_timeout)
        };
        let mut state = self.state.lock().expect("admission capacity lock poisoned");
        if state.closed {
            return;
        }
        state.limits = limits;
        state.queue_timeout = queue_timeout;
        self.changes.send_replace(());
    }

    /// Admits against current limits, preserving FIFO order and each waiter's original timeout.
    pub(crate) async fn admit(
        &self,
        request: &AdmissionRequest,
        context: &AdmissionContext<'_>,
    ) -> Result<AdmissionPermit, AdmissionError> {
        let units = request.units().ok_or(AdmissionError::BatchTooLarge)?;
        let deadline = context.deadline;
        let mut changes = self.changes.subscribe();
        let (queued, expires) = {
            let mut state = self.state.lock().expect("admission capacity lock poisoned");
            if state.closed {
                return Err(AdmissionError::Closed);
            }
            if units == 0
                || state
                    .limits
                    .is_some_and(|limits| units > limits.concurrent_work_units)
            {
                return Err(AdmissionError::BatchTooLarge);
            }
            let now = tokio::time::Instant::now();
            if now >= deadline {
                return Err(AdmissionError::DeadlineExceeded);
            }
            if state.limits.is_none_or(|limits| {
                state.waiters.is_empty()
                    && state.active + u64::from(units) <= u64::from(limits.concurrent_work_units)
            }) {
                state.active += u64::from(units);
                return Ok(AdmissionPermit::new(Reservation::new(
                    self.clone(),
                    units,
                    context.metrics.active.clone(),
                    None,
                )));
            }
            let limits = state.limits.expect("unrestricted admission never queues");
            if state.queued + u64::from(units) > u64::from(limits.queued_work_units) {
                return Err(AdmissionError::Overloaded);
            }
            let token = Arc::new(());
            state.waiters.push_back(token.clone());
            state.queued += u64::from(units);
            let expires = state
                .queue_timeout
                .and_then(|duration| now.checked_add(duration))
                .map_or(deadline, |queue_deadline| queue_deadline.min(deadline));
            changes.borrow_and_update();
            (
                Reservation::new(
                    self.clone(),
                    units,
                    context.metrics.queued.clone(),
                    Some(token),
                ),
                expires,
            )
        };
        let _waiting = context.queue.begin_wait();
        loop {
            {
                let mut state = self.state.lock().expect("admission capacity lock poisoned");
                changes.borrow_and_update();
                if state.closed {
                    return Err(AdmissionError::Closed);
                }
                // A previously valid batch invalidated by a limit reduction is unavailable,
                // not a malformed new request. Removing its guard lets later waiters proceed.
                if state
                    .limits
                    .is_some_and(|limits| units > limits.concurrent_work_units)
                {
                    return Err(AdmissionError::Closed);
                }
                if tokio::time::Instant::now() >= expires {
                    return Err(if expires == deadline {
                        AdmissionError::DeadlineExceeded
                    } else {
                        AdmissionError::QueueTimeout
                    });
                }
                let first = state.waiters.front().is_some_and(|token| {
                    Arc::ptr_eq(
                        token,
                        queued
                            .token
                            .as_ref()
                            .expect("queued reservation has a token"),
                    )
                });
                if first
                    && state.limits.is_none_or(|limits| {
                        state.active + u64::from(units) <= u64::from(limits.concurrent_work_units)
                    })
                {
                    state.active += u64::from(units);
                    return Ok(AdmissionPermit::new(Reservation::new(
                        self.clone(),
                        units,
                        context.metrics.active.clone(),
                        None,
                    )));
                }
            }
            tokio::select! {
                biased;
                _ = tokio::time::sleep_until(expires) => {},
                _ = changes.changed() => {},
            }
        }
    }

    /// Closes this owner permanently, waking waiters without revoking running reservations.
    pub(crate) fn close(&self) {
        self.state
            .lock()
            .expect("admission capacity lock poisoned")
            .closed = true;
        self.changes.send_replace(());
    }
}

// Each guard releases only its remaining work units. Queue cancellation removes its FIFO token;
// splitting an active reservation transfers already-counted ownership without acquiring again.
struct Reservation {
    owner: AdmissionCapacityState,
    units: u32,
    gauge: Gauge,
    token: Option<Arc<()>>,
}

impl Reservation {
    fn new(
        owner: AdmissionCapacityState,
        units: u32,
        gauge: Gauge,
        token: Option<Arc<()>>,
    ) -> Self {
        gauge.inc_by(i64::from(units));
        Self {
            owner,
            units,
            gauge,
            token,
        }
    }
}

impl AdmissionReservation for Reservation {
    fn split_one(&mut self) -> Box<dyn AdmissionReservation> {
        assert!(
            self.token.is_none() && self.units > 0,
            "only active batch units can be split"
        );
        self.units -= 1;
        Box::new(Self {
            owner: self.owner.clone(),
            units: 1,
            gauge: self.gauge.clone(),
            token: None,
        })
    }
}

impl Drop for Reservation {
    fn drop(&mut self) {
        let mut state = self
            .owner
            .state
            .lock()
            .expect("admission capacity lock poisoned");
        if let Some(token) = &self.token {
            let position = state
                .waiters
                .iter()
                .position(|entry| Arc::ptr_eq(entry, token))
                .expect("queued reservation owns a FIFO entry");
            state.waiters.remove(position);
            state.queued -= u64::from(self.units);
        } else {
            state.active -= u64::from(self.units);
        }
        self.gauge.dec_by(i64::from(self.units));
        self.owner.changes.send_replace(());
    }
}
