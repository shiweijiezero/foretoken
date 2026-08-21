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

pub(crate) fn load(candidate: &RouteCandidate) -> i64 {
    candidate
        .route_target_stats
        .as_ref()
        .and_then(|stats| i64::try_from(stats.running_requests).ok())
        .unwrap_or(0)
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
