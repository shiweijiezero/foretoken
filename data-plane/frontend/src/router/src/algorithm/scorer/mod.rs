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
    running_request_scorer => RunningRequestScorer = "running_request",
    queue_depth_scorer => QueueDepthScorer = "queue_depth",
    uniform_scorer => UniformScorer = "uniform",
}

/// Converts request-count observations into inverse min-max preferences.
pub(super) fn inverse_normalized_scores(
    counts: impl IntoIterator<Item = Option<u64>>,
) -> Vec<RouteScore> {
    let counts = counts.into_iter().collect::<Vec<_>>();
    let range = counts
        .iter()
        .flatten()
        .copied()
        .min()
        .zip(counts.iter().flatten().copied().max());
    counts
        .into_iter()
        .map(|count| RouteScore {
            preference: match count.zip(range) {
                None => -1.0,
                Some((_, (minimum, maximum))) if maximum == minimum => 1.0,
                // Subtract integer counts before conversion to preserve large adjacent differences.
                Some((count, (minimum, maximum))) => {
                    (maximum - count) as f64 / (maximum - minimum) as f64
                }
            },
            ..RouteScore::default()
        })
        .collect()
}

/// Scores the complete filtered compatible, healthy route target snapshot for one routing round.
///
/// The returned score slice is parallel to `candidates`: position `n` scores candidate `n`. This
/// lets a scorer express ranking without echoing candidate identity or metadata. The Router
/// applies execution-stage and E/P/D route-set eligibility only after scores are available.
///
/// - `request`: model, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `candidates`: Filter output with route metadata, candidate-specific future pipeline stages,
///   and the Router's immutable current-round group and rank observations, when available.
/// - `kv_prefix_indexer`: query local or offloaded matched prompt tokens for any candidate.
/// - `routing_progress`: immutable E/P/D selection round and progress supplied by `RouteSession`.
/// - `customized_context`: user-defined `C`, created per request and shared across E/P/D rounds.
///
/// Returns one score for every input candidate. A length mismatch is reported as a routing error.
pub trait RouteScorer<C: Send + 'static = ()>: Send + Sync {
    /// Requests live shared-prefix observations before the synchronous routing round.
    fn needs_kv_prefix(&self) -> bool {
        false
    }

    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut C,
    ) -> Vec<RouteScore>;
}

/// Returns this DP rank's scheduler load; unavailable observations rank after measured loads.
/// Group-level admission counts cannot be assigned to an individual rank.
pub(crate) fn load(candidate: &RouteCandidate) -> i64 {
    candidate
        .data_parallel_stats()
        .and_then(|stats| {
            Some(
                stats
                    .scheduler_running_requests?
                    .saturating_add(stats.scheduler_waiting_requests?),
            )
        })
        .and_then(|requests| i64::try_from(requests).ok())
        .unwrap_or(i64::MAX)
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
