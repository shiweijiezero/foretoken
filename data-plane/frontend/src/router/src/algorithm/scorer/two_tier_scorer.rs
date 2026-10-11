// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Selecting by load imbalance, then device-cache affinity.

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, RoutingProgress};
use foretoken_kv_indexer::{KvPrefixIndexer, KvPrefixQueryResult};
use foretoken_model_protocol::{KvCacheLocality, KvStorageTier, ModelServerRole};
use serde::Deserialize;

/// Marks the two-tier winner for the `max` picker, preserving candidate-order ties.
#[derive(Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct TwoTierScorer {
    cache_threshold: f64,
    balance_abs_threshold: usize,
    balance_rel_threshold: f64,
}

impl Default for TwoTierScorer {
    fn default() -> Self {
        Self {
            cache_threshold: 0.5,
            balance_abs_threshold: 32,
            balance_rel_threshold: 1.1,
        }
    }
}

impl TwoTierScorer {
    /// Selects within the current execution stage using one shared KV-block unit.
    /// Different observed block sizes leave candidates tied without a cache-dependent preference.
    fn select(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
    ) -> Option<usize> {
        let eligible = candidates
            .iter()
            .enumerate()
            .filter(|(_, candidate)| candidate.stage_eligible);
        let least_loaded = eligible
            .clone()
            .min_by_key(|(_, candidate)| candidate.local_load.requests)?;
        let min_load = least_loaded.1.local_load.requests as usize;
        let max_load = eligible
            .clone()
            .map(|(_, candidate)| candidate.local_load.requests as usize)
            .max()?;
        if max_load.saturating_sub(min_load) > self.balance_abs_threshold
            && max_load as f64 > self.balance_rel_threshold * min_load as f64
        {
            return Some(least_loaded.0);
        }

        let overlaps = eligible
            .clone()
            .map(|(row, candidate)| {
                let cache = device_overlap(request, candidate, kv_prefix_indexer);
                (row, cache)
            })
            .collect::<Vec<_>>();
        let max_overlap = overlaps
            .iter()
            .map(|(_, cache)| cache.map_or(0, |(blocks, _)| blocks))
            .max()?;
        if max_overlap == 0 {
            return Some(least_loaded.0);
        }
        let block_size = overlaps
            .iter()
            .find_map(|(_, cache)| cache.map(|(_, size)| size))?;
        if overlaps
            .iter()
            .any(|(_, cache)| cache.is_some_and(|(_, size)| size != block_size))
        {
            return None;
        }
        let request_blocks = request.token_count().div_ceil(block_size);
        let cache_ratio = if request_blocks == 0 {
            0.0
        } else {
            max_overlap as f64 / request_blocks as f64
        };
        if cache_ratio > self.cache_threshold {
            overlaps
                .iter()
                .filter(|(_, cache)| cache.is_some_and(|(blocks, _)| blocks == max_overlap))
                .min_by_key(|(row, _)| candidates[*row].local_load.requests)
                .map(|(row, _)| *row)
        } else {
            Some(least_loaded.0)
        }
    }
}

impl RouteScorer for TwoTierScorer {
    /// Requests live device-prefix observations before selection.
    fn needs_kv_prefix(&self) -> bool {
        true
    }

    /// Validates and stores the two-tier thresholds at pipeline startup.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        let config: Self = serde_json::from_value(parameters).map_err(|error| error.to_string())?;
        if !config.cache_threshold.is_finite() || !(0.0..=1.0).contains(&config.cache_threshold) {
            return Err("cache_threshold must be finite and between 0 and 1".into());
        }
        if !config.balance_rel_threshold.is_finite() || config.balance_rel_threshold < 1.0 {
            return Err("balance_rel_threshold must be finite and at least 1".into());
        }
        *self = config;
        Ok(())
    }

    /// Gives the selected row one and other rows zero; pair with `max` for exact selection.
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        let selected = self.select(request, candidates, kv_prefix_indexer);
        candidates
            .iter()
            .enumerate()
            .map(|(row, _)| match selected {
                Some(selected) => RouteScore {
                    preference: if row == selected { 1.0 } else { 0.0 },
                    ..RouteScore::default()
                },
                None => RouteScore::default(),
            })
            .collect()
    }
}

/// Returns complete Device-prefix blocks and their source block size for one exact route binding.
fn device_overlap(
    request: &RouterRequest,
    candidate: &RouteCandidate,
    indexer: &dyn KvPrefixIndexer,
) -> Option<(usize, usize)> {
    if candidate.role == ModelServerRole::Encoder {
        return None;
    }
    let lookup = request
        .kv_prefix_lookup(
            candidate.route_target_id.as_str(),
            candidate.data_parallel_rank,
        )
        .ok()?;
    let KvPrefixQueryResult::Matches(matches) = indexer.prefix_matches(lookup) else {
        return None;
    };
    let block_size = matches.block_size()? as usize;
    let total_blocks = request.token_count() / block_size;
    let blocks = matches
        .into_iter()
        .filter(|matched| {
            matched.placement.tier == KvStorageTier::Device
                && matched.placement.locality != KvCacheLocality::Unspecified
        })
        .map(|matched| (matched.matched_tokens / block_size).min(total_blocks))
        .max()
        .unwrap_or(0);
    Some((blocks, block_size))
}
