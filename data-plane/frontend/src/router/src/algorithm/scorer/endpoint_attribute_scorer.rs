// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Linear scoring of an observed endpoint attribute.

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};
use foretoken_kv_indexer::KvPrefixIndexer;
use serde::Deserialize;

/// Normalizes one configured observation into candidate preferences for Router selection.
#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct EndpointAttributeScorer {
    attribute_key: EndpointAttribute,
    algorithm: Algorithm,
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum EndpointAttribute {
    #[default]
    SchedulerWaitingRequests,
    SchedulerRunningRequests,
    KvCacheUsage,
    PromptTokensPerSecond,
    GenerationTokensPerSecond,
    TtftAverageMs,
    TtftP95Ms,
    TpotAverageMs,
    TpotP95Ms,
    E2eLatencyAverageMs,
    E2eLatencyP95Ms,
}

impl EndpointAttributeScorer {
    /// Reads rank-local gauges or group-level window statistics without substituting missing data.
    fn read_attribute(&self, candidate: &RouteCandidate) -> Option<f64> {
        let stats = candidate.route_target_stats.as_ref()?;
        match self.attribute_key {
            EndpointAttribute::SchedulerWaitingRequests => candidate
                .data_parallel_stats()?
                .scheduler_waiting_requests
                .map(|value| value as f64),
            EndpointAttribute::SchedulerRunningRequests => candidate
                .data_parallel_stats()?
                .scheduler_running_requests
                .map(|value| value as f64),
            EndpointAttribute::KvCacheUsage => candidate.data_parallel_stats()?.kv_cache_usage,
            EndpointAttribute::PromptTokensPerSecond => stats.prompt_tokens_per_second,
            EndpointAttribute::GenerationTokensPerSecond => stats.generation_tokens_per_second,
            EndpointAttribute::TtftAverageMs => Some(stats.ttft.as_ref()?.average_ms),
            EndpointAttribute::TtftP95Ms => stats.ttft.as_ref()?.p95_ms,
            EndpointAttribute::TpotAverageMs => Some(stats.tpot.as_ref()?.average_ms),
            EndpointAttribute::TpotP95Ms => stats.tpot.as_ref()?.p95_ms,
            EndpointAttribute::E2eLatencyAverageMs => Some(stats.e2e_latency.as_ref()?.average_ms),
            EndpointAttribute::E2eLatencyP95Ms => stats.e2e_latency.as_ref()?.p95_ms,
        }
    }
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct Algorithm {
    #[serde(rename = "type")]
    direction: Direction,
    #[serde(default)]
    normalization: Normalization,
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "snake_case")]
enum Direction {
    #[default]
    LinearLowerIsBetter,
    LinearHigherIsBetter,
}

#[derive(Default, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Normalization {
    fixed_range: Option<FixedRange>,
    adaptive_range: Option<AdaptiveRange>,
}

#[derive(Default, Deserialize)]
#[serde(default, deny_unknown_fields)]
struct FixedRange {
    min: f64,
    max: f64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AdaptiveRange {}

impl RouteScorer for EndpointAttributeScorer {
    /// Validates the attribute and normalization parameters once at pipeline startup.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        let config: Self = serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        let normalization = &config.algorithm.normalization;
        if normalization.fixed_range.is_some() && normalization.adaptive_range.is_some() {
            return Err("normalization accepts only one of fixedRange and adaptiveRange".into());
        }
        if let Some(range) = &normalization.fixed_range
            && range.min >= range.max
        {
            return Err("normalization.fixedRange requires min < max".into());
        }
        *self = config;
        Ok(())
    }

    /// Returns linear preferences in candidate order; missing observations receive zero.
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        let values = candidates
            .iter()
            .map(|candidate| self.read_attribute(candidate))
            .collect::<Vec<_>>();
        let fixed = self.algorithm.normalization.fixed_range.as_ref();
        let (minimum, maximum) = fixed.map_or_else(
            || {
                // Later pipeline stages are visible here but are not selectable endpoints.
                values
                    .iter()
                    .zip(candidates)
                    .filter(|(_, candidate)| candidate.stage_eligible)
                    .filter_map(|(value, _)| value.as_ref())
                    .fold((f64::INFINITY, f64::NEG_INFINITY), |(min, max), &value| {
                        // Use the upstream comparisons, including NaN and signed-zero behavior.
                        (
                            if value < min { value } else { min },
                            if value > max { value } else { max },
                        )
                    })
            },
            |range| (range.min, range.max),
        );
        values
            .into_iter()
            .map(|value| {
                let preference = match value {
                    None => 0.0,
                    Some(_) if fixed.is_none() && maximum == minimum => 1.0,
                    Some(value) => {
                        let normalized = (value - minimum) / (maximum - minimum);
                        let normalized = if fixed.is_some() {
                            // Go's math.Max(0, math.Min(1, x)) canonicalizes negative zero.
                            if normalized <= 0.0 {
                                0.0
                            } else {
                                normalized.clamp(0.0, 1.0)
                            }
                        } else {
                            normalized
                        };
                        match self.algorithm.direction {
                            Direction::LinearLowerIsBetter => 1.0 - normalized,
                            Direction::LinearHigherIsBetter => normalized,
                        }
                    }
                };
                RouteScore { preference }
            })
            .collect()
    }
}
