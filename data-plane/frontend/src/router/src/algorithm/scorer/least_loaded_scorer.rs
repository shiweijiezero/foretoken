// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by current route target load.

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use super::{decode_loads_by_pipeline_scope, load};
use std::sync::Arc;

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, ScorerDescriptor};

inventory::submit! {
    ScorerDescriptor {
        name: "least_loaded",
        factory: || Arc::new(LeastLoadedScorer),
    }
}

/// Prefers lower current load and includes the least-loaded Decode node when scoring Prefill.
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
        // A Prefill eligible route option includes the lightest Decode load in its own E/P/D route set.
        // The score compares E/P/D route sets without prematurely binding to one Decode rank.
        let decode_loads = decode_loads_by_pipeline_scope(candidates);

        candidates
            .iter()
            .map(|candidate| {
                let downstream_load = if candidate.role == ModelServerRole::Prefill {
                    decode_loads
                        .get(&candidate.pipeline_scope_id)
                        .copied()
                        .unwrap_or(0)
                } else {
                    0
                };
                // Picker prefers larger scores, so negate the total load: less load ranks higher.
                RouteScore {
                    matched_tokens: 0,
                    tier_preference: 0,
                    locality_preference: 0,
                    load: load(candidate)
                        .saturating_add(downstream_load)
                        .saturating_neg(),
                }
            })
            .collect()
    }
}
