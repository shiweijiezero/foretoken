// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by in-flight and incoming uncached prompt tokens.

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};
use foretoken_kv_indexer::KvPrefixIndexer;
use serde::Deserialize;

/// Scores accumulated in-flight tokens plus the current request's uncached prompt tokens.
#[derive(Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
pub struct TokenLoadScorer {
    queue_threshold_tokens: i64,
}

impl Default for TokenLoadScorer {
    fn default() -> Self {
        Self {
            queue_threshold_tokens: 4_194_304,
        }
    }
}
impl RouteScorer for TokenLoadScorer {
    /// Requests live prefix observations before cache-aware scoring.
    fn needs_kv_prefix(&self) -> bool {
        true
    }

    /// Applies token-load parameters at pipeline startup, defaulting nonpositive thresholds.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        *self = serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        if self.queue_threshold_tokens <= 0 {
            self.queue_threshold_tokens = Self::default().queue_threshold_tokens;
        }
        Ok(())
    }

    /// Returns token-load preferences in candidate order for Router selection.
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
                let uncached =
                    crate::inflight::uncached_tokens(request, candidate, kv_prefix_indexer) as i64;
                // Add signed token counts before converting to floating point.
                let tokens = candidate.inflight.tokens.wrapping_add(uncached) as f64;
                let score = if tokens <= 0.0 {
                    1.0
                } else {
                    1.0 - tokens.min(self.queue_threshold_tokens as f64)
                        / self.queue_threshold_tokens as f64
                };
                RouteScore::new(score)
            })
            .collect()
    }
}
