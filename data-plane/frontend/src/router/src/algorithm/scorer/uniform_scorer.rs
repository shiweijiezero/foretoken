// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Built-in scorer that assigns every candidate the same score.

use foretoken_kv_indexer::KvPrefixIndexer;

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, ScoredCandidate};

/// Assigns the same score to every candidate.
#[derive(Default)]
pub struct UniformScorer;

impl RouteScorer for UniformScorer {
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: Vec<RouteCandidate>,
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        route_target_stats_reader: &dyn crate::RouteTargetStatsReader,
        customized_context: &mut (),
    ) -> Vec<ScoredCandidate> {
        candidates
            .into_iter()
            .map(|candidate| ScoredCandidate {
                candidate,
                score: RouteScore::default(),
            })
            .collect()
    }
}
