// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by current route target load.

use foretoken_kv_indexer::KvPrefixIndexer;

use super::least_loaded_scores;
use std::sync::Arc;

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, ScorerDescriptor};

inventory::submit! {
    ScorerDescriptor {
        name: "least_loaded",
        factory: || Arc::new(LeastLoadedScorer),
    }
}

/// Prefers lower current load and includes the least-loaded required downstream E/P/D stages.
#[derive(Default)]
pub struct LeastLoadedScorer;

impl RouteScorer for LeastLoadedScorer {
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        least_loaded_scores(candidates)
    }
}
