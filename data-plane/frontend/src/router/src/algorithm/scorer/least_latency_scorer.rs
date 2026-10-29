// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring prefill and decode latency at the eligible target-average request length.

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};
use foretoken_kv_indexer::KvPrefixIndexer;
use std::collections::BTreeSet;

/// Prefers lower expected latency using daily request lengths and cumulative latency means.
#[derive(Default)]
pub struct LeastLatencyScorer;

impl RouteScorer for LeastLatencyScorer {
    fn needs_request_length_history(&self) -> bool {
        true
    }

    /// Ranks normalized latency and explores idle ranks until request observations are available.
    fn score(
        &self,
        _request: &RouterRequest,
        candidates: &[RouteCandidate],
        _kv_prefix_indexer: &dyn KvPrefixIndexer,
        _routing_progress: &RoutingProgress<'_>,
        _customized_context: &mut (),
    ) -> Vec<RouteScore> {
        // Metrics belong to targets, so expanding DP ranks must not reweight their means.
        // Length observations contribute even when another metric leaves that candidate unscored.
        let mut seen_targets = BTreeSet::new();
        let mut prompt_sum = 0.0;
        let mut prompt_count = 0;
        let mut generation_sum = 0.0;
        let mut generation_count = 0;
        for candidate in candidates {
            if !candidate.stage_eligible || !seen_targets.insert(&candidate.route_target_id) {
                continue;
            }
            let Some(stats) = &candidate.route_target_stats else {
                continue;
            };
            if let Some(mean) = stats.prompt_tokens_per_request
                && mean > 0.0
            {
                prompt_sum += mean;
                prompt_count += 1;
            }
            if let Some(mean) = stats.generation_tokens_per_request
                && mean > 0.0
            {
                generation_sum += mean;
                generation_count += 1;
            }
        }
        let guess_prompt_tokens = if prompt_count > 0 {
            prompt_sum / prompt_count as f64
        } else {
            10.0
        };
        let guess_generation_tokens = if generation_count > 0 {
            generation_sum / generation_count as f64
        } else {
            100.0
        };
        candidates
            .iter()
            .map(|candidate| {
                let cost = candidate.route_target_stats.as_ref().and_then(|stats| {
                    let metric = &stats.request_cost;
                    let prompt = stats.prompt_tokens_per_request?;
                    let generation = stats.generation_tokens_per_request?;
                    let avg_prompt_tokens = if prompt > 0.0 {
                        prompt
                    } else {
                        guess_prompt_tokens
                    };
                    let avg_generation_tokens = if generation > 0.0 {
                        generation
                    } else {
                        guess_generation_tokens
                    };
                    let prefill = metric.prefill_seconds.filter(|moments| moments.count > 0)?;
                    let decode = metric.decode_seconds.filter(|moments| moments.count > 0)?;
                    let prefill_latency = prefill.mean() / avg_prompt_tokens * guess_prompt_tokens;
                    let decode_latency =
                        decode.mean() / avg_generation_tokens * guess_generation_tokens;
                    let cost = prefill_latency + decode_latency;
                    cost.is_finite().then_some(cost)
                });
                // Measured costs retain their ordering. Router reservations allow one exploratory
                // request per idle rank while telemetry catches up with completed requests.
                let preference = match cost {
                    Some(cost) => 1.0 / (1.0 + cost),
                    None if candidate.local_load.requests == 0 => 2.0,
                    None => -(candidate.local_load.requests as f64),
                };
                RouteScore { preference }
            })
            .collect()
    }
}
