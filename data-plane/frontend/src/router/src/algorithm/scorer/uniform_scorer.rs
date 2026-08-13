// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scorer that assigns every candidate the same score.

use foretoken_kv_indexer::KvPrefixIndexer;

use std::sync::Arc;

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, ScorerDescriptor};

inventory::submit! {
    ScorerDescriptor {
        name: "uniform",
        factory: || Arc::new(UniformScorer),
    }
}

/// Assigns the same score to every candidate.
#[derive(Default)]
pub struct UniformScorer;

impl RouteScorer for UniformScorer {
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        vec![RouteScore::default(); candidates.len()]
    }
}
