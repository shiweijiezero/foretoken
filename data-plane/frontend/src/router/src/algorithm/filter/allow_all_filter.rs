// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Built-in filter that preserves every eligible candidate.

use foretoken_kv_indexer::KvPrefixIndexer;

use crate::{RouteCandidate, RouteFilter, RouterRequest};

/// Keeps every candidate produced by the Router's health and compatibility checks.
#[derive(Default)]
pub struct AllowAllFilter;

impl RouteFilter for AllowAllFilter {
    #[allow(unused_variables)]
    fn filter(
        &self,
        request: &RouterRequest,
        candidates: Vec<RouteCandidate>,
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        route_target_stats_reader: &dyn crate::RouteTargetStatsReader,
        customized_context: &mut (),
    ) -> Vec<RouteCandidate> {
        candidates
    }
}
