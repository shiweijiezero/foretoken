// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scoring by KV-prefix locality and current load.

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use super::{decode_loads_by_pipeline_scope, load};
use std::sync::Arc;

use crate::{RouteCandidate, RouteScore, RouteScorer, RouterRequest, ScorerDescriptor};

inventory::submit! {
    ScorerDescriptor {
        name: "kv_least_loaded",
        factory: || Arc::new(KvLeastLoadedScorer),
    }
}

/// Prefers longer confirmed-locality KV-prefix matches, then lower current and downstream Decode load.
#[derive(Default)]
pub struct KvLeastLoadedScorer;

impl RouteScorer for KvLeastLoadedScorer {
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv: &dyn KvPrefixIndexer,
        _: &mut (),
    ) -> Vec<RouteScore> {
        let decode_loads = decode_loads_by_pipeline_scope(candidates);
        candidates
            .iter()
            .map(|candidate| {
                let (tokens, tier, locality) = if matches!(
                    candidate.role,
                    ModelServerRole::Aggregate | ModelServerRole::Prefill
                ) {
                    // The lookup borrows prompt data and uses the candidate's exact replica; it does
                    // not clone or mutate the client generation request.
                    let lookup = foretoken_kv_indexer::KvPrefixLookup::from_generate_request(
                        candidate.route_target_id.as_str(),
                        candidate.data_parallel_rank,
                        request.generate_request.as_ref(),
                    );
                    match lookup.map_or_else(
                        foretoken_kv_indexer::KvPrefixQueryResult::Unavailable,
                        |lookup| kv.prefix_matches(lookup),
                    ) {
                        foretoken_kv_indexer::KvPrefixQueryResult::Matches(matches) => matches
                            // Providers outside this crate are not trusted to have applied indexer
                            // filtering: unknown locality is equivalent to no cache match.
                            .into_iter()
                            .filter(|m| {
                                m.placement.locality
                                    != foretoken_model_protocol::KvCacheLocality::Unspecified
                            })
                            .max_by_key(|m| {
                                (
                                    m.matched_tokens,
                                    tier_preference(m.placement.tier),
                                    locality_preference(m.placement.locality),
                                )
                            })
                            .map(|m| {
                                (
                                    m.matched_tokens,
                                    tier_preference(m.placement.tier),
                                    locality_preference(m.placement.locality),
                                )
                            })
                            .unwrap_or((0, 0, 0)),
                        foretoken_kv_indexer::KvPrefixQueryResult::Unavailable(_) => (0, 0, 0),
                    }
                } else {
                    // Decode consumes generated tokens, not the prompt KV prefix.
                    (0, 0, 0)
                };
                let downstream = if candidate.role == ModelServerRole::Prefill {
                    decode_loads
                        .get(&candidate.pipeline_scope_id)
                        .copied()
                        .unwrap_or(0)
                } else {
                    0
                };
                RouteScore {
                    matched_tokens: i64::try_from(tokens).unwrap_or(i64::MAX),
                    tier_preference: tier,
                    locality_preference: locality,
                    load: load(candidate).saturating_add(downstream).saturating_neg(),
                }
            })
            .collect()
    }
}
/// Lexicographic policy deliberately has no measured weights.
fn tier_preference(t: foretoken_model_protocol::KvStorageTier) -> i8 {
    match t {
        foretoken_model_protocol::KvStorageTier::Device => 4,
        foretoken_model_protocol::KvStorageTier::HostPinned => 3,
        foretoken_model_protocol::KvStorageTier::Disk => 2,
        foretoken_model_protocol::KvStorageTier::External => 1,
    }
}

/// Lexicographic policy deliberately has no measured weights.
fn locality_preference(locality: foretoken_model_protocol::KvCacheLocality) -> i8 {
    match locality {
        // The scorer filters this value before ranking; retain zero for exhaustive typed handling.
        foretoken_model_protocol::KvCacheLocality::Unspecified => 0,
        foretoken_model_protocol::KvCacheLocality::Local => 2,
        foretoken_model_protocol::KvCacheLocality::Remote => 1,
    }
}
