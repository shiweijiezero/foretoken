// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Candidate scoring contract and built-in scorers.

mod kv_least_loaded_scorer;
mod least_loaded_scorer;
mod uniform_scorer;

use std::collections::BTreeMap;

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use crate::{RouteCandidate, RouteTargetStatsReader, RouterRequest, ScoredCandidate};

pub use kv_least_loaded_scorer::KvLeastLoadedScorer;
pub use least_loaded_scorer::LeastLoadedScorer;
pub use uniform_scorer::UniformScorer;

/// Scores the complete filtered compatible, healthy route target snapshot for one routing round.
///
/// Each output must retain the identity and metadata of an input candidate; scorers must not
/// introduce or fabricate candidates. The pipeline applies stage/domain eligibility only after
/// scores are available.
///
/// - `request`: model, optional revision, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `candidates`: Filter output with route target ID, target, role, model, revision, and current load.
/// - `kv_prefix_indexer`: query local or offloaded matched prompt tokens for any candidate.
/// - `route_target_stats_reader`: query load, scheduler, KV usage, throughput, and latency for a chosen
///   `Duration`.
/// - `customized_context`: user-defined `C`, created per request and shared by Prefill and Decode.
///
/// Returns one `ScoredCandidate` for each candidate passed to the Picker.
pub trait RouteScorer<C: Send + 'static = ()>: Send + Sync {
    fn score(
        &self,
        request: &RouterRequest,
        candidates: Vec<RouteCandidate>,
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        route_target_stats_reader: &dyn RouteTargetStatsReader,
        customized_context: &mut C,
    ) -> Vec<ScoredCandidate>;
}

pub(crate) fn load(candidate: &RouteCandidate) -> i64 {
    candidate
        .route_target_load
        .as_ref()
        .and_then(|value| value.running_requests)
        .and_then(|value| i64::try_from(value).ok())
        .unwrap_or(0)
}

/// Returns the least route-target load among Decode candidates in each execution domain.
pub(crate) fn decode_loads_by_domain(
    candidates: &[RouteCandidate],
) -> BTreeMap<Option<String>, i64> {
    let mut loads = BTreeMap::new();
    for candidate in candidates
        .iter()
        .filter(|candidate| candidate.role == ModelServerRole::Decode)
    {
        loads
            .entry(candidate.domain_id.clone())
            .and_modify(|current: &mut i64| *current = (*current).min(load(candidate)))
            .or_insert_with(|| load(candidate));
    }
    loads
}
