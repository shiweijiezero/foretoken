// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by requests currently owned by this frontend.

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};
use foretoken_kv_indexer::KvPrefixIndexer;
use serde::Deserialize;

/// Gives idle endpoints full preference and scales busy endpoints against the maximum count.
#[derive(Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
pub struct ActiveRequestScorer {
    idle_threshold: i64,
    max_busy_score: Option<f64>,
}

impl Default for ActiveRequestScorer {
    fn default() -> Self {
        Self {
            idle_threshold: 0,
            max_busy_score: Some(1.0),
        }
    }
}
impl RouteScorer for ActiveRequestScorer {
    /// Applies idle and busy scoring parameters at pipeline startup with their default semantics.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        *self = serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        self.idle_threshold = self.idle_threshold.max(0);
        if self
            .max_busy_score
            .is_some_and(|score| !(0.0..=1.0).contains(&score))
        {
            self.max_busy_score = Some(1.0);
        }
        Ok(())
    }

    /// Returns idle and busy request preferences in candidate order for Router selection.
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        let maximum = candidates
            .iter()
            .map(|candidate| candidate.inflight.requests)
            .max()
            .unwrap_or(0)
            .max(0);
        candidates
            .iter()
            .map(|candidate| {
                let count = candidate.inflight.requests;
                RouteScore::new(if count <= self.idle_threshold {
                    1.0
                } else {
                    (maximum - count) as f64 / maximum as f64 * self.max_busy_score.unwrap_or(1.0)
                })
            })
            .collect()
    }
}
