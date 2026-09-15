// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by complete cached prompt blocks and matched prefix length.

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};
use foretoken_kv_indexer::KvPrefixIndexer;
use serde::Deserialize;

/// Scores complete cache blocks by hit ratio and an optional squared match-length term.
#[derive(Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
pub struct PrefixScorer {
    match_length_weight: f64,
    match_length_scale_tokens: i64,
}

impl Default for PrefixScorer {
    fn default() -> Self {
        Self {
            match_length_weight: 0.0,
            match_length_scale_tokens: 8192,
        }
    }
}

impl RouteScorer for PrefixScorer {
    /// Requests live prefix observations before cache-aware scoring.
    fn needs_kv_prefix(&self) -> bool {
        true
    }

    /// Validates prefix weights at pipeline startup and stores the scorer's parameters.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        let config: Self = serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        if !(0.0..=1.0).contains(&config.match_length_weight) {
            return Err("matchLengthWeight must be between 0 and 1".into());
        }
        if config.match_length_weight > 0.0 && config.match_length_scale_tokens <= 0 {
            return Err(
                "matchLengthScaleTokens must be positive when matchLengthWeight is positive".into(),
            );
        }
        *self = config;
        Ok(())
    }

    /// Returns prefix-match preferences in candidate order for Router selection.
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        candidates
            .iter()
            .map(|candidate| {
                let Some(info) = crate::cache::cache_match(request, candidate, kv_prefix_indexer)
                else {
                    return RouteScore::new(0.0);
                };
                if info.total_blocks == 0 {
                    return RouteScore::new(0.0);
                }
                let ratio = info.matched_blocks as f64 / info.total_blocks as f64;
                let mut length = 0.0;
                if self.match_length_weight > 0.0 && info.block_size > 0 {
                    let normalized = (info.matched_blocks as f64 * info.block_size as f64
                        / self.match_length_scale_tokens as f64)
                        .min(1.0);
                    length = normalized * normalized;
                }
                RouteScore::new(
                    self.match_length_weight * length + (1.0 - self.match_length_weight) * ratio,
                )
            })
            .collect()
    }
}
