// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Reusable prefix-token observations for request-specific routing costs.

use crate::{RouteCandidate, RouterRequest};
use foretoken_kv_indexer::{KvPrefixIndexer, KvPrefixQueryResult};
use foretoken_model_protocol::{KvCacheLocality, KvStorageTier, ModelServerRole};

pub(crate) struct CacheMatch {
    pub(crate) matched_tokens: usize,
    pub(crate) device_tokens: usize,
    pub(crate) total_tokens: usize,
    pub(crate) host_tokens: usize,
    pub(crate) disk_tokens: usize,
    pub(crate) shared_tokens: usize,
}

/// Reads reusable token counts from the exact route binding for routing costs.
/// The observation owner applies cache-layout alignment and final-token recomputation.
pub(crate) fn cache_match(
    request: &RouterRequest,
    candidate: &RouteCandidate,
    kv: &dyn KvPrefixIndexer,
) -> Option<CacheMatch> {
    if candidate.role == ModelServerRole::Encoder {
        return None;
    }
    let lookup = request
        .kv_prefix_lookup(
            candidate.route_target_id.as_str(),
            candidate.data_parallel_rank,
        )
        .ok()?;
    let KvPrefixQueryResult::Matches(matches) = kv.prefix_matches(lookup) else {
        return None;
    };
    let mut info = CacheMatch {
        matched_tokens: 0,
        device_tokens: 0,
        total_tokens: request.token_count(),
        host_tokens: 0,
        disk_tokens: 0,
        shared_tokens: 0,
    };
    for matched in matches {
        if matched.placement.locality == KvCacheLocality::Unspecified {
            continue;
        }
        let count = matched.matched_tokens.min(info.total_tokens);
        info.matched_tokens = info.matched_tokens.max(count);
        match (matched.placement.tier, matched.placement.locality) {
            (KvStorageTier::Device, KvCacheLocality::Local) => {
                info.device_tokens = info.device_tokens.max(count);
            }
            (KvStorageTier::HostPinned, KvCacheLocality::Local) => {
                info.host_tokens = info.host_tokens.max(count);
            }
            (KvStorageTier::Disk, KvCacheLocality::Local) => {
                info.disk_tokens = info.disk_tokens.max(count);
            }
            (KvStorageTier::External, _) => info.shared_tokens = info.shared_tokens.max(count),
            _ => {}
        }
    }
    Some(info)
}
