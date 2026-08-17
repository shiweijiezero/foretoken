// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Candidate-list filtering and Filter implementations.

mod allow_all_filter;

use foretoken_kv_indexer::KvPrefixIndexer;

use crate::{CandidateIndex, RouteCandidate, RouterRequest};

pub use allow_all_filter::AllowAllFilter;

/// Filters the complete compatible, healthy route target snapshot for one routing round.
///
/// A filter returns positions from `candidates`, which permits it to retain any subset without
/// returning candidate identities or metadata. The Router applies execution-stage and E/P/D
/// route-set eligibility after scoring.
///
/// - `request`: model, optional revision, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `candidates`: routable ModelGroups with route metadata and the Router's immutable current-round
///   aggregate target observation, when telemetry is available.
/// - `kv_prefix_indexer`: query local or offloaded matched prompt tokens for any candidate.
/// - `customized_context`: user-defined `C`, created per request and shared by Prefill and Decode.
///
/// Returns indexes of candidates that may continue to scoring. Out-of-range or duplicate indexes
/// are reported as routing errors.
pub trait RouteFilter<C: Send + 'static = ()>: Send + Sync {
    fn filter(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        customized_context: &mut C,
    ) -> Vec<CandidateIndex>;
}
