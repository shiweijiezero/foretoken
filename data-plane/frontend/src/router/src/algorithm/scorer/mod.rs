// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Candidate scoring and Scorer implementations.

mod kv_least_loaded_scorer;
mod least_loaded_scorer;
mod uniform_scorer;

use std::collections::BTreeMap;

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use crate::{RouteCandidate, RouteScore, RouterRequest};

pub use kv_least_loaded_scorer::KvLeastLoadedScorer;
pub use least_loaded_scorer::LeastLoadedScorer;
pub use uniform_scorer::UniformScorer;

/// Scores the complete filtered compatible, healthy route target snapshot for one routing round.
///
/// The returned score slice is parallel to `candidates`: position `n` scores candidate `n`. This
/// lets a scorer express ranking without echoing candidate identity or metadata. The Router
/// applies execution-stage and E/P/D route-set eligibility only after scores are available.
///
/// - `request`: model, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `candidates`: Filter output with route metadata and the Router's immutable current-round
///   aggregate target observation, when telemetry is available.
/// - `kv_prefix_indexer`: query local or offloaded matched prompt tokens for any candidate.
/// - `customized_context`: user-defined `C`, created per request and shared by Prefill and Decode.
///
/// Returns one score for every input candidate. A length mismatch is reported as a routing error.
pub trait RouteScorer<C: Send + 'static = ()>: Send + Sync {
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        customized_context: &mut C,
    ) -> Vec<RouteScore>;
}

/// Returns the best available view of a candidate's current engine request load.
///
/// Model-server admission and vLLM scheduler gauges overlap, so the load is their maximum rather
/// than their sum. A missing route-target snapshot receives the largest penalty instead of looking
/// like an idle target.
pub(crate) fn load(candidate: &RouteCandidate) -> i64 {
    candidate
        .route_target_stats
        .as_ref()
        .map_or(i64::MAX, |stats| {
            let scheduler_requests = stats
                .scheduler_running_requests
                .zip(stats.scheduler_waiting_requests)
                .map(|(running, waiting)| running.saturating_add(waiting));
            let requests = scheduler_requests
                .map(|scheduler| stats.running_requests.max(scheduler))
                .unwrap_or(stats.running_requests);
            i64::try_from(requests).unwrap_or(i64::MAX)
        })
}

/// Returns the least model-server load among Decode options in each E/P/D pipeline scope.
pub(crate) fn decode_loads_by_pipeline_scope(
    candidates: &[RouteCandidate],
) -> BTreeMap<Option<String>, i64> {
    role_min_by_pipeline_scope(candidates, ModelServerRole::Decode, |candidate| {
        Some(load(candidate))
    })
    .expect("candidate load is always available")
}

/// Produces the built-in least-loaded ranking for callers that must fall back from incomplete
/// telemetry without comparing values expressed in different units.
pub(crate) fn least_loaded_scores(candidates: &[RouteCandidate]) -> Vec<RouteScore> {
    pipeline_sum_penalties(candidates, |candidate| Some(load(candidate)))
        .expect("candidate load is always available")
        .into_iter()
        .map(metric_score)
        .collect()
}

/// Sums one comparable non-negative stage penalty across each candidate's executable pipeline.
///
/// Downstream alternatives contribute their minimum penalty because later routing rounds may
/// select the best compatible target in the chosen pipeline scope. `None` rejects the complete
/// round so unknown observations are never compared with measured values.
pub(crate) fn pipeline_sum_penalties(
    candidates: &[RouteCandidate],
    metric: impl Fn(&RouteCandidate) -> Option<i64>,
) -> Option<Vec<i64>> {
    let values = candidates.iter().map(&metric).collect::<Option<Vec<_>>>()?;
    let prefill = role_min_by_pipeline_scope(candidates, ModelServerRole::Prefill, &metric)?;
    let decode = role_min_by_pipeline_scope(candidates, ModelServerRole::Decode, &metric)?;
    Some(
        candidates
            .iter()
            .zip(values)
            .map(|(candidate, own)| {
                let downstream_prefill = prefill
                    .get(&candidate.pipeline_scope_id)
                    .copied()
                    .unwrap_or(0);
                let downstream_decode = decode
                    .get(&candidate.pipeline_scope_id)
                    .copied()
                    .unwrap_or(0);
                match candidate.role {
                    ModelServerRole::Encoder => own
                        .saturating_add(downstream_prefill)
                        .saturating_add(downstream_decode),
                    ModelServerRole::Prefill => own.saturating_add(downstream_decode),
                    ModelServerRole::Aggregate | ModelServerRole::Decode => own,
                }
            })
            .collect(),
    )
}

/// Returns the smallest available metric for `role` in each E/P/D pipeline scope.
fn role_min_by_pipeline_scope(
    candidates: &[RouteCandidate],
    role: ModelServerRole,
    metric: impl Fn(&RouteCandidate) -> Option<i64>,
) -> Option<BTreeMap<Option<String>, i64>> {
    let mut values = BTreeMap::new();
    for candidate in candidates.iter().filter(|candidate| candidate.role == role) {
        let value = metric(candidate)?;
        values
            .entry(candidate.pipeline_scope_id.clone())
            .and_modify(|current: &mut i64| *current = (*current).min(value))
            .or_insert(value);
    }
    Some(values)
}

/// Converts one non-negative metric penalty into a pure `RouteScore` where lower is better.
pub(crate) fn metric_score(penalty: i64) -> RouteScore {
    RouteScore {
        matched_tokens: 0,
        tier_preference: 0,
        locality_preference: 0,
        load: penalty.saturating_neg(),
    }
}
