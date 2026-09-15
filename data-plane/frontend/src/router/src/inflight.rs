// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Frontend-owned request lifecycle and request-specific load projections.

use crate::{RouteCandidate, RouteTargetId, RouterRequest};
use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;
use std::collections::BTreeMap;
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
    /// Uncached prompt tokens awaiting the first response, including Decode dispatches.
    pub tokens: i64,
}

#[derive(Default)]
pub(crate) struct InFlightRequests {
    requests: BTreeMap<RequestKey, BTreeMap<String, usize>>,
}

impl InFlightRequests {
    /// Takes a coherent candidate snapshot under the routing transaction's lock.
    pub(crate) fn snapshot(&self, key: &RequestKey) -> InFlightLoad {
        let Some(requests) = self.requests.get(key) else {
            return InFlightLoad::default();
        };
        InFlightLoad {
            requests: requests.len() as i64,
            tokens: requests.values().fold(0_i64, |tokens, request| {
                tokens.wrapping_add(*request as i64)
            }),
        }
    }

    /// Reserves a selected stage before dispatch; the owning session or stream must release it.
    pub(crate) fn insert(
        &mut self,
        request: &RouterRequest,
        candidate: &RouteCandidate,
        kv: &dyn KvPrefixIndexer,
    ) {
        let key = (
            candidate.route_target_id.clone(),
            candidate.data_parallel_rank,
        );
        // Reserve uncached prompt tokens for each generation stage until its first response.
        // Encoder has no token-generation workload.
        let uncached_tokens = if candidate.role == ModelServerRole::Encoder {
            0
        } else {
            uncached_tokens(request, candidate, kv)
        };
        self.requests.entry(key).or_default().insert(
            request.generate_request.request_id.as_str().to_owned(),
            uncached_tokens,
        );
    }

    /// Releases prompt-token load when the first response reaches the frontend.
    pub(crate) fn output(&mut self, key: &RequestKey, id: &str) {
        if let Some(request) = self
            .requests
            .get_mut(key)
            .and_then(|requests| requests.get_mut(id))
        {
            *request = 0;
        }
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

/// Returns uncached indexed tokens and the partial prompt tail for routing load accounting.
pub(crate) fn uncached_tokens(
    request: &RouterRequest,
    candidate: &RouteCandidate,
    kv: &dyn KvPrefixIndexer,
) -> usize {
    let Some(info) = crate::cache::cache_match(request, candidate, kv) else {
        return request.token_count();
    };
    let indexed = info.total_blocks * info.block_size;
    indexed.saturating_sub(info.matched_blocks * info.block_size)
        + request.token_count().saturating_sub(indexed)
}
