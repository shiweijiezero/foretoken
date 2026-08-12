// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Candidate-list filtering contract and built-in filters.

mod allow_all_filter;

use foretoken_kv_indexer::KvPrefixIndexer;

use crate::{RouteCandidate, RouteTargetStatsReader, RouterRequest};

pub use allow_all_filter::AllowAllFilter;

/// Filters the complete compatible, healthy route target snapshot for one routing round.
///
/// Implementations may only remove input candidates; retained candidates preserve their identity
/// and metadata, and filters must not introduce or fabricate candidates. The pipeline owns
/// eligibility and later stage/domain narrowing.
///
/// - `request`: model, optional revision, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `candidates`: routable ModelGroups with ID, scaling target, role, model, revision, and current load.
/// - `kv_prefix_indexer`: query local or offloaded matched prompt tokens for any candidate.
/// - `route_target_stats_reader`: query load, scheduler, KV usage, throughput, and latency for a chosen
///   `Duration`.
/// - `customized_context`: user-defined `C`, created per request and shared by Prefill and Decode.
///
/// Returns the candidates that may continue to scoring.
pub trait RouteFilter<C: Send + 'static = ()>: Send + Sync {
    fn filter(
        &self,
        request: &RouterRequest,
        candidates: Vec<RouteCandidate>,
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        route_target_stats_reader: &dyn RouteTargetStatsReader,
        customized_context: &mut C,
    ) -> Vec<RouteCandidate>;
}
