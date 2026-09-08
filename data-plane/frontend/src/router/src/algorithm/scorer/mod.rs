// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Candidate scoring and Scorer implementations.

use std::collections::BTreeMap;

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use crate::{RouteCandidate, RouteScore, RouterRequest, RoutingProgress};

// Each entry declares the module, re-exports the implementation, and binds its user-facing Scorer name.
// For example, `kv_least_loaded_scorer => KvLeastLoadedScorer = "kv_least_loaded"` maps
// `kv_least_loaded_scorer.rs`, the `KvLeastLoadedScorer` type, and the user-facing name.
declare_router_algorithms! {
    descriptor = ScorerDescriptor;
    kv_cache_utilization_scorer => KvCacheUtilizationScorer = "kv_cache_utilization",
    kv_least_loaded_scorer => KvLeastLoadedScorer = "kv_least_loaded",
    least_loaded_scorer => LeastLoadedScorer = "least_loaded",
    queue_depth_scorer => QueueDepthScorer = "queue_depth",
    uniform_scorer => UniformScorer = "uniform",
}

/// Scores the complete filtered compatible, healthy route target snapshot for one routing round.
///
/// The returned score slice is parallel to `candidates`: position `n` scores candidate `n`. This
/// lets a scorer express ranking without echoing candidate identity or metadata. The Router
/// applies execution-stage and E/P/D route-set eligibility only after scores are available.
///
/// - `request`: model, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `candidates`: Filter output with route metadata, candidate-specific future pipeline stages,
///   and the Router's immutable current-round aggregate target observation, when available.
/// - `kv_prefix_indexer`: query local or offloaded matched prompt tokens for any candidate.
/// - `routing_progress`: immutable E/P/D selection round and progress supplied by `RouteSession`.
/// - `customized_context`: user-defined `C`, created per request and shared across E/P/D rounds.
///
/// Returns one score for every input candidate. A length mismatch is reported as a routing error.
pub trait RouteScorer<C: Send + 'static = ()>: Send + Sync {
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut C,
    ) -> Vec<RouteScore>;
}

/// Returns the best available view of a candidate's current engine request load.
///
/// Model-server admission and vLLM scheduler gauges overlap, so the load is their maximum rather
/// than their sum. Built-in load scorers consume this derived value; the candidate retains its
/// telemetry snapshot.
pub(crate) fn load(candidate: &RouteCandidate) -> i64 {
    candidate.route_target_stats.as_ref().map_or(0, |stats| {
        let scheduler_requests = stats
            .scheduler_running_requests
            .unwrap_or(0)
            .saturating_add(stats.scheduler_waiting_requests.unwrap_or(0));
        let requests = stats.running_requests.max(scheduler_requests);
        i64::try_from(requests).unwrap_or(i64::MAX)
    })
}

/// Returns the least model-server route load among Decode eligible route options in each E/P/D route set.
pub(crate) fn decode_loads_by_pipeline_scope(
    candidates: &[RouteCandidate],
) -> BTreeMap<Option<String>, i64> {
    let mut loads = BTreeMap::new();
    for candidate in candidates
        .iter()
        .filter(|candidate| candidate.role == ModelServerRole::Decode)
    {
        loads
            .entry(candidate.pipeline_scope_id.clone())
            .and_modify(|current: &mut i64| *current = (*current).min(load(candidate)))
            .or_insert_with(|| load(candidate));
    }
    loads
}
