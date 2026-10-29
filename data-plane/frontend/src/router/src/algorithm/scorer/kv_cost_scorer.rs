// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring prefill and decode load in comparable token units with cache-reuse credit.

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};
use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;
use serde::Deserialize;

/// Disjoint worker-local cache tiers and an independent shared-cache prefix, in tokens.
#[derive(Default)]
struct CacheCostInput {
    device_tokens: f64,
    host_tokens: f64,
    disk_tokens: f64,
    shared_tokens: f64,
}

impl CacheCostInput {
    /// Converts indexed prefix lengths into non-overlapping worker tiers for the cost model.
    fn read(request: &RouterRequest, candidate: &RouteCandidate, kv: &dyn KvPrefixIndexer) -> Self {
        let Some(cache) = crate::cache::cache_match(request, candidate, kv) else {
            return Self::default();
        };
        let device = cache.device_tokens;
        let host = cache.host_tokens;
        let disk = cache.disk_tokens;
        Self {
            device_tokens: device as f64,
            host_tokens: host.saturating_sub(device) as f64,
            disk_tokens: disk.saturating_sub(device.max(host)) as f64,
            shared_tokens: cache.shared_tokens as f64,
        }
    }
}

/// Scores frontend-local prefill work and active KV tokens with configurable cache credit.
#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct KvCostScorer {
    overlap_score_credit: f64,
    overlap_score_credit_decay: f64,
    prefill_load_scale: f64,
    decode_active_request_weight: f64,
    host_cache_hit_weight: f64,
    disk_cache_hit_weight: f64,
    shared_cache_multiplier: f64,
    router_track_prefill_tokens: bool,
    router_track_output_blocks: bool,
}

/// Ranks an available KV cost above candidates without block-size metadata.
fn cost_score(cost: Option<f64>) -> RouteScore {
    RouteScore {
        preference: cost.map_or(f64::NEG_INFINITY, |cost| -cost),
    }
}

impl Default for KvCostScorer {
    fn default() -> Self {
        Self {
            overlap_score_credit: 1.0,
            overlap_score_credit_decay: 0.0,
            prefill_load_scale: 1.0,
            decode_active_request_weight: 0.0,
            host_cache_hit_weight: 0.75,
            disk_cache_hit_weight: 0.25,
            shared_cache_multiplier: 0.0,
            router_track_prefill_tokens: true,
            router_track_output_blocks: false,
        }
    }
}

impl KvCostScorer {
    /// Estimates avoided prefill work for reservation and scoring, excluding shared-cache credit.
    fn cached_tokens(&self, cache: &CacheCostInput) -> usize {
        let effective_tokens = cache.device_tokens
            + cache.host_tokens * self.host_cache_hit_weight
            + cache.disk_tokens * self.disk_cache_hit_weight;
        effective_tokens.round().max(0.0) as usize
    }

    fn tracks_prefill(&self, candidate: &RouteCandidate) -> bool {
        self.router_track_prefill_tokens
            && matches!(
                candidate.role,
                ModelServerRole::Aggregate | ModelServerRole::Prefill
            )
    }

    /// Computes one token-equivalent cost from the current routing snapshot and prepared cache input.
    fn worker_cost(
        &self,
        request: &RouterRequest,
        candidate: &RouteCandidate,
        cache: &CacheCostInput,
        block_size: usize,
        min_active_prefill_tokens: usize,
    ) -> f64 {
        let decode_cost = crate::routing_load::potential_decode_tokens(
            &candidate.active_sequences,
            request,
            block_size,
            candidate.role != ModelServerRole::Decode
                && candidate.prefix_cache_enabled == Some(true),
            self.router_track_output_blocks,
        ) as f64;
        let active_request_cost =
            self.decode_active_request_weight * candidate.local_load.requests.max(0) as f64;
        // Decode follows completed Prefill and has no remaining prompt computation.
        if candidate.role == ModelServerRole::Decode {
            return decode_cost + active_request_cost;
        }
        let track_prefill = self.tracks_prefill(candidate);
        let active_prefill_tokens = candidate.local_load.tokens.max(0) as usize;
        let overlap_credit_decay = if track_prefill && self.overlap_score_credit_decay > 0.0 {
            let excess_tokens = active_prefill_tokens.saturating_sub(min_active_prefill_tokens);
            let normalized_load = excess_tokens as f64 / request.token_count().max(1) as f64;
            1.0 / (1.0 + self.overlap_score_credit_decay * normalized_load)
        } else {
            1.0
        };
        let effective_credit = self.overlap_score_credit * overlap_credit_decay;
        // Shared hits cover a contiguous prefix and receive credit only beyond device hits.
        // Host and disk counts already exclude all faster worker-local tiers.
        let shared_overlap =
            self.shared_cache_multiplier * (cache.shared_tokens - cache.device_tokens).max(0.0);
        let overlap_credit = effective_credit * cache.device_tokens
            + self.host_cache_hit_weight * cache.host_tokens
            + self.disk_cache_hit_weight * cache.disk_tokens
            + shared_overlap;
        let raw_prefill_tokens = if track_prefill {
            active_prefill_tokens.saturating_add(request.token_count())
        } else {
            0
        };
        self.prefill_load_scale * (raw_prefill_tokens as f64 - overlap_credit).max(0.0)
            + decode_cost
            + active_request_cost
    }
}

impl RouteScorer for KvCostScorer {
    fn tracks_output_blocks(&self) -> bool {
        self.router_track_output_blocks
    }

    fn needs_active_sequences(&self) -> bool {
        true
    }

    /// Requests cache observations only when the cost model enables cache credit.
    fn needs_kv_prefix(&self) -> bool {
        self.overlap_score_credit != 0.0
    }

    /// Reserves effective prefill tokens until the first response; Decode has no prompt work left.
    fn prefill_token_load(
        &self,
        request: &RouterRequest,
        candidate: &RouteCandidate,
        kv_prefix_indexer: &dyn KvPrefixIndexer,
    ) -> usize {
        if !self.tracks_prefill(candidate) {
            return 0;
        }
        if !self.needs_kv_prefix() {
            return request.token_count();
        }
        let cache = CacheCostInput::read(request, candidate, kv_prefix_indexer);
        request
            .token_count()
            .saturating_sub(self.cached_tokens(&cache))
    }

    /// Applies cost weights once at pipeline construction, rejecting invalid numeric parameters.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        let config: Self = serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        for (name, value) in [
            ("overlap_score_credit", config.overlap_score_credit),
            (
                "overlap_score_credit_decay",
                config.overlap_score_credit_decay,
            ),
            ("prefill_load_scale", config.prefill_load_scale),
            (
                "decode_active_request_weight",
                config.decode_active_request_weight,
            ),
            ("host_cache_hit_weight", config.host_cache_hit_weight),
            ("disk_cache_hit_weight", config.disk_cache_hit_weight),
            ("shared_cache_multiplier", config.shared_cache_multiplier),
        ] {
            if !value.is_finite() || value < 0.0 {
                return Err(format!("{name} must be finite and non-negative"));
            }
        }
        *self = config;
        Ok(())
    }

    /// Returns comparable token costs in candidate order, using only eligible load minima.
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        _routing_progress: &RoutingProgress<'_>,
        _customized_context: &mut (),
    ) -> Vec<RouteScore> {
        let minimum = if self.router_track_prefill_tokens && self.overlap_score_credit_decay > 0.0 {
            candidates
                .iter()
                .filter(|candidate| {
                    candidate.stage_eligible
                        && candidate.kv_block_size.is_some()
                        && self.tracks_prefill(candidate)
                })
                .map(|candidate| candidate.local_load.tokens.max(0) as usize)
                .min()
                .unwrap_or(0)
        } else {
            0
        };
        candidates
            .iter()
            .map(|candidate| {
                if candidate.role == ModelServerRole::Encoder {
                    return cost_score(None);
                }
                let Some(block_size) = candidate.kv_block_size else {
                    return cost_score(None);
                };
                let size = block_size.get() as usize;
                // Load-only policies neither credit cache tiers nor discount reservations.
                let cache = if self.needs_kv_prefix() && candidate.role != ModelServerRole::Decode {
                    CacheCostInput::read(request, candidate, kv_prefix_indexer)
                } else {
                    CacheCostInput::default()
                };
                cost_score(Some(
                    self.worker_cost(request, candidate, &cache, size, minimum),
                ))
            })
            .collect()
    }
}
