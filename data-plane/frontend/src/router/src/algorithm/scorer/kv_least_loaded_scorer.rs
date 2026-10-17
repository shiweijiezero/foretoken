// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Balances GPU-local and restored prefix affinity against rank-local request pressure.

use std::collections::BTreeMap;

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;
use serde::Deserialize;

use super::scheduler_requests;
use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};

/// Combines cache affinity and request pressure for aggregate and prefill selection.
/// Encoder and Decode selection use load alone; parameters are owned by this scorer.
#[derive(Deserialize)]
#[serde(default, rename_all = "camelCase", deny_unknown_fields)]
pub struct KvLeastLoadedScorer {
    cache_weight: f64,
    restore_weight: f64,
}

impl Default for KvLeastLoadedScorer {
    fn default() -> Self {
        Self {
            cache_weight: 0.5,
            restore_weight: 0.5,
        }
    }
}

impl RouteScorer for KvLeastLoadedScorer {
    /// Requests live prefix observations before cache-aware scoring.
    fn needs_kv_prefix(&self) -> bool {
        self.cache_weight > 0.0
    }

    /// Validates affinity weights when the routing pipeline is built or reconfigured.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        let config: Self = serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        for (name, value) in [
            ("cacheWeight", config.cache_weight),
            ("restoreWeight", config.restore_weight),
        ] {
            if !(0.0..=1.0).contains(&value) {
                return Err(format!("{name} must be between 0 and 1"));
            }
        }
        *self = config;
        Ok(())
    }

    /// Scores each candidate without modifying reservations or cache state.
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        _routing_progress: &RoutingProgress<'_>,
        _customized_context: &mut (),
    ) -> Vec<RouteScore> {
        // Prefill considers the lightest observed Decode option in its own pipeline.
        // Decode need not be eligible in this round; its actual selection occurs later.
        let mut decode_pressure = BTreeMap::<_, u64>::new();
        for candidate in candidates
            .iter()
            .filter(|candidate| candidate.role == ModelServerRole::Decode)
        {
            if let Some(pressure) = request_pressure(candidate) {
                decode_pressure
                    .entry(candidate.pipeline_scope_id.as_deref())
                    .and_modify(|current| *current = (*current).min(pressure))
                    .or_insert(pressure);
            }
        }

        candidates
            .iter()
            .map(|candidate| {
                let pressure = request_pressure(candidate).and_then(|current| {
                    if candidate.role == ModelServerRole::Prefill {
                        decode_pressure
                            .get(&candidate.pipeline_scope_id.as_deref())
                            .map(|downstream| current.saturating_add(*downstream))
                    } else {
                        Some(current)
                    }
                });
                // Missing telemetry earns no low-load credit. Reciprocal pressure does not
                // change when unrelated or currently ineligible candidates enter the snapshot.
                let load_preference = pressure.map_or(0.0, |load| 1.0 / (1.0 + load as f64));
                if self.cache_weight == 0.0
                    || !matches!(
                        candidate.role,
                        ModelServerRole::Aggregate | ModelServerRole::Prefill
                    )
                {
                    return RouteScore {
                        preference: load_preference,
                    };
                }

                let cache_preference =
                    crate::cache::cache_match(request, candidate, kv_prefix_indexer)
                        .filter(|cache| cache.total_tokens > 0)
                        .map_or(0.0, |cache| {
                            // Both values are complete model-prefix boundaries. Discounting
                            // the restored extension expresses affinity, not transfer bytes or time.
                            let credit = cache.device_tokens as f64
                                + self.restore_weight
                                    * (cache.matched_tokens - cache.device_tokens) as f64;
                            credit / cache.total_tokens as f64
                        });
                RouteScore {
                    preference: self.cache_weight * cache_preference
                        + (1.0 - self.cache_weight) * load_preference,
                }
            })
            .collect()
    }
}

/// Combines engine observation and this frontend's reservations without adding overlapping requests.
fn request_pressure(candidate: &RouteCandidate) -> Option<u64> {
    scheduler_requests(candidate).map(|native| native.max(candidate.local_load.requests as u64))
}
