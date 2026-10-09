// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Bounded concurrency and FIFO waiting shared by successive routing generations.

use std::sync::Arc;
use std::time::Duration;

use serde::Deserialize;
use tokio::sync::{OwnedSemaphorePermit, Semaphore, TryAcquireError};

use prometheus_client::metrics::gauge::Gauge;

use crate::{
    AdmissionCapacity, AdmissionContext, AdmissionError, AdmissionPermit, AdmissionRequest,
    AdmissionReservation, AdmissionRule,
};

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Parameters {
    max_concurrent_requests: u32,
    #[serde(default)]
    max_queued_requests: u32,
    queue_timeout: Option<String>,
}

/// Concurrency and FIFO queue capacity for one model in a frontend.
/// The frontend retains this owner across serving-snapshot replacements.
pub struct ConcurrencyAdmission {
    capacity: u32,
    queue_capacity: u32,
    active: Arc<Semaphore>,
    queued: Arc<Semaphore>,
    queue_timeout: Option<Duration>,
}

impl ConcurrencyAdmission {
    /// Resolves the concurrency stage once at process startup, without guessing capacity.
    pub fn from_parameters(parameters: serde_json::Value) -> Result<Self, String> {
        let parameters: Parameters = serde_json::from_value(parameters)
            .map_err(|error| format!("admission.parameters: {error}"))?;
        let capacity = parameters.max_concurrent_requests;
        if capacity == 0 {
            return Err("admission.parameters.maxConcurrentRequests must be positive".into());
        }
        if capacity as usize > Semaphore::MAX_PERMITS
            || parameters.max_queued_requests as usize > Semaphore::MAX_PERMITS
        {
            return Err("admission request limits exceed supported capacity".into());
        }
        let queue_timeout = parameters
            .queue_timeout
            .map(|value| {
                humantime::parse_duration(&value)
                    .map_err(|error| format!("admission.parameters.queueTimeout: {error}"))
                    .and_then(|duration| {
                        if duration.is_zero() {
                            Err("admission.parameters.queueTimeout must be positive".into())
                        } else {
                            Ok(duration)
                        }
                    })
            })
            .transpose()?;
        Ok(Self {
            capacity,
            queue_capacity: parameters.max_queued_requests,
            active: Arc::new(Semaphore::new(capacity as usize)),
            queued: Arc::new(Semaphore::new(parameters.max_queued_requests as usize)),
            queue_timeout,
        })
    }
}

#[async_trait::async_trait]
impl AdmissionRule for ConcurrencyAdmission {
    fn capacity(&self) -> Option<AdmissionCapacity> {
        Some(AdmissionCapacity {
            concurrent_work_units: self.capacity,
            queued_work_units: self.queue_capacity,
        })
    }

    fn requires_ready_runtime(&self) -> bool {
        true
    }

    async fn admit(
        &self,
        request: &AdmissionRequest,
        context: &AdmissionContext<'_>,
    ) -> Result<AdmissionPermit, AdmissionError> {
        let units = request.units().ok_or(AdmissionError::BatchTooLarge)?;
        let deadline = context.deadline;
        if units == 0 || units > self.capacity {
            return Err(AdmissionError::BatchTooLarge);
        }
        if tokio::time::Instant::now() >= deadline {
            return Err(AdmissionError::DeadlineExceeded);
        }
        match self.active.clone().try_acquire_many_owned(units) {
            Ok(permit) => return Ok(Reservation::counted(permit, context.metrics.active.clone())),
            Err(TryAcquireError::Closed) => return Err(AdmissionError::Closed),
            Err(TryAcquireError::NoPermits) => {}
        }
        let queued =
            self.queued
                .clone()
                .try_acquire_many_owned(units)
                .map_err(|error| match error {
                    TryAcquireError::Closed => AdmissionError::Closed,
                    TryAcquireError::NoPermits => AdmissionError::Overloaded,
                })?;
        let started = tokio::time::Instant::now();
        let _queued = Reservation::counted(queued, context.metrics.queued.clone());
        let _waiting = context.queue.begin_wait();
        let expires = self
            .queue_timeout
            .and_then(|duration| started.checked_add(duration))
            .map_or(deadline, |queue_deadline| queue_deadline.min(deadline));
        let permit = tokio::select! {
            biased;
            _ = tokio::time::sleep_until(expires) => None,
            permit = self.active.clone().acquire_many_owned(units) => {
                Some(permit.map_err(|_| AdmissionError::Closed)?)
            }
        };
        // A ready semaphore can win before the timer driver observes an elapsed deadline.
        match permit {
            Some(permit) if tokio::time::Instant::now() < expires => {
                Ok(Reservation::counted(permit, context.metrics.active.clone()))
            }
            _ if expires == deadline => Err(AdmissionError::DeadlineExceeded),
            _ => Err(AdmissionError::QueueTimeout),
        }
    }

    fn close(&self) {
        self.queued.close();
        self.active.close();
    }
}

// Each guard accounts only for its remaining units; split transfers already-counted ownership.
struct Reservation {
    permit: OwnedSemaphorePermit,
    gauge: Gauge,
}

impl Reservation {
    fn counted(permit: OwnedSemaphorePermit, gauge: Gauge) -> AdmissionPermit {
        let reservation = Self { permit, gauge };
        reservation
            .gauge
            .inc_by(reservation.permit.num_permits() as i64);
        AdmissionPermit::new(reservation)
    }
}

impl AdmissionReservation for Reservation {
    fn split_one(&mut self) -> Box<dyn AdmissionReservation> {
        Box::new(Self {
            permit: self
                .permit
                .split(1)
                .expect("generation batch has a reserved unit for each child"),
            gauge: self.gauge.clone(),
        })
    }
}

impl Drop for Reservation {
    fn drop(&mut self) {
        self.gauge.dec_by(self.permit.num_permits() as i64);
    }
}
