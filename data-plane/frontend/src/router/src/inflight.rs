// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Frontend-owned request lifecycle and request-specific load projections.

use crate::{RouteCandidate, RouteTargetId, RouterRequest};

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

/// Shared local request accounting retained by RuntimeBuilder across serving-snapshot updates.
/// Sessions from retiring generations continue releasing their own contributions into this state.
#[derive(Clone, Default)]
pub struct RoutingLoadState(pub(crate) Arc<Mutex<InFlightRequests>>);

pub(crate) type RequestKey = (RouteTargetId, u32);

/// Snapshot of locally routed requests; engine telemetry is deliberately not added to these counts.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct InFlightLoad {
    /// Selected requests still owned by a stage or response stream.
    pub requests: i64,
}

#[derive(Default)]
pub(crate) struct InFlightRequests {
    requests: BTreeMap<RequestKey, BTreeSet<String>>,
}

impl InFlightRequests {
    /// Takes a coherent candidate snapshot under the routing transaction's lock.
    pub(crate) fn snapshot(&self, key: &RequestKey) -> InFlightLoad {
        let Some(requests) = self.requests.get(key) else {
            return InFlightLoad::default();
        };
        InFlightLoad {
            requests: requests.len() as i64,
        }
    }

    /// Reserves a selected stage before dispatch; the owning session or stream must release it.
    pub(crate) fn insert(&mut self, request: &RouterRequest, candidate: &RouteCandidate) {
        let key = (
            candidate.route_target_id.clone(),
            candidate.data_parallel_rank,
        );
        self.requests
            .entry(key)
            .or_default()
            .insert(request.generate_request.request_id.as_str().to_owned());
    }

    /// Removes exactly one request-stage contribution on completion, rejection, or cancellation.
    pub(crate) fn remove(&mut self, key: &RequestKey, id: &str) {
        if let Some(requests) = self.requests.get_mut(key) {
            requests.remove(id);
            if requests.is_empty() {
                self.requests.remove(key);
            }
        }
    }
}
