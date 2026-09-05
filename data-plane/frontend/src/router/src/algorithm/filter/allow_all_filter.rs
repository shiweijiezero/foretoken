// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Filter that preserves every eligible candidate.

use foretoken_kv_indexer::KvPrefixIndexer;

use crate::{CandidateIndex, RouteCandidate, RouteFilter, RouterRequest};

/// Keeps every candidate produced by the Router's health and compatibility checks.
#[derive(Default)]
pub struct AllowAllFilter;

impl RouteFilter for AllowAllFilter {
    #[allow(unused_variables)]
    fn filter(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        customized_context: &mut (),
    ) -> Vec<CandidateIndex> {
        (0..candidates.len()).map(CandidateIndex).collect()
    }
}
