// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Configured concurrency and FIFO waiting backed by a stable work-unit owner.

use serde::Deserialize;

use crate::{
    AdmissionCapacity, AdmissionCapacityState, AdmissionContext, AdmissionError, AdmissionPermit,
    AdmissionRequest, AdmissionRule,
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
/// The registry retains the capacity owner across built-in rule updates.
pub struct ConcurrencyAdmission {
    capacity: AdmissionCapacityState,
}

impl ConcurrencyAdmission {
    /// Parses a candidate's limits without changing the published owner's reservations or queue.
    pub fn from_parameters(parameters: serde_json::Value) -> Result<Self, String> {
        let parameters: Parameters = serde_json::from_value(parameters)
            .map_err(|error| format!("admission.parameters: {error}"))?;
        let capacity = parameters.max_concurrent_requests;
        if capacity == 0 {
            return Err("admission.parameters.maxConcurrentRequests must be positive".into());
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
            capacity: AdmissionCapacityState::new(
                Some(AdmissionCapacity {
                    concurrent_work_units: capacity,
                    queued_work_units: parameters.max_queued_requests,
                }),
                queue_timeout,
            ),
        })
    }
}

#[async_trait::async_trait]
impl AdmissionRule for ConcurrencyAdmission {
    fn capacity(&self) -> Option<AdmissionCapacity> {
        self.capacity.capacity()
    }

    fn capacity_state(&self) -> Option<&AdmissionCapacityState> {
        Some(&self.capacity)
    }

    fn requires_ready_runtime(&self) -> bool {
        self.capacity.capacity().is_some()
    }

    async fn admit(
        &self,
        request: &AdmissionRequest,
        context: &AdmissionContext<'_>,
    ) -> Result<AdmissionPermit, AdmissionError> {
        self.capacity.admit(request, context).await
    }

    fn close(&self) {
        self.capacity.close();
    }
}
