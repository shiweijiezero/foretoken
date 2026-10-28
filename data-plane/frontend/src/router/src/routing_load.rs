// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Frontend-owned routing reservations and request-specific load projections.

use crate::{RouteCandidate, RouteTargetId, RouterRequest, RoutingLoadSnapshot};
use foretoken_kv_indexer::KvPrefixIndexer;
use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};

/// Shared routing reservations retained by RuntimeBuilder across serving-snapshot updates.
/// Sessions from retiring generations continue releasing their own reservations into this state.
#[derive(Clone, Default)]
pub struct RoutingLoadState(pub(crate) Arc<Mutex<RoutingReservations>>);

impl RoutingLoadState {
    /// Reads this frontend's current reservations for one target and rank without modifying them.
    pub fn snapshot(&self, target: &RouteTargetId, rank: u32) -> RoutingLoadSnapshot {
        self.0
            .lock()
            .expect("routing load lock poisoned")
            .snapshot(&(target.clone(), rank))
    }
}

pub(crate) type ReservationKey = (RouteTargetId, u32);

#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ActiveSequence {
    request: Option<Arc<vllm_llm::GenerateRequest>>,
    prefix_shareable: bool,
    prefill_tokens: usize,
    output_tokens: usize,
}

#[derive(Default)]
pub(crate) struct RoutingReservations {
    requests: BTreeMap<ReservationKey, BTreeMap<String, ActiveSequence>>,
}

impl RoutingReservations {
    /// Takes a coherent candidate snapshot under the routing transaction's lock.
    pub(crate) fn snapshot(&self, key: &ReservationKey) -> RoutingLoadSnapshot {
        let Some(requests) = self.requests.get(key) else {
            return RoutingLoadSnapshot::default();
        };
        RoutingLoadSnapshot {
            requests: requests.len() as i64,
            tokens: requests.values().fold(0_i64, |tokens, request| {
                tokens.wrapping_add(request.prefill_tokens as i64)
            }),
        }
    }

    /// Copies active sequences for the scorer's block projection while the routing lock is held.
    pub(crate) fn active_sequences(&self, key: &ReservationKey) -> Vec<ActiveSequence> {
        self.requests
            .get(key)
            .map(|requests| requests.values().cloned().collect())
            .unwrap_or_default()
    }

    /// Reserves a selected stage before dispatch; the owning session or stream must release it.
    pub(crate) fn reserve(
        &mut self,
        request: &RouterRequest,
        candidate: &RouteCandidate,
        prefill_tokens: usize,
    ) {
        let key = (
            candidate.route_target_id.clone(),
            candidate.data_parallel_rank,
        );
        self.requests.entry(key).or_default().insert(
            request.request_id().to_owned(),
            ActiveSequence {
                request: request.generate_request.clone(),
                prefix_shareable: request.reads_prefix_cache()
                    && candidate.prefix_cache_enabled == Some(true),
                prefill_tokens,
                output_tokens: 0,
            },
        );
    }

    /// Records stream-observed output against the stage until its reservation is released.
    pub(crate) fn record_output_tokens(&mut self, key: &ReservationKey, id: &str, count: usize) {
        if let Some(request) = self
            .requests
            .get_mut(key)
            .and_then(|requests| requests.get_mut(id))
        {
            request.output_tokens = request.output_tokens.saturating_add(count);
        }
    }

    /// Releases prompt-token load when the first response reaches the frontend.
    pub(crate) fn release_prompt_load(&mut self, key: &ReservationKey, id: &str) {
        if let Some(request) = self
            .requests
            .get_mut(key)
            .and_then(|requests| requests.get_mut(id))
        {
            request.prefill_tokens = 0;
        }
    }

    /// Removes exactly one request-stage contribution on completion, rejection, or cancellation.
    pub(crate) fn release(&mut self, key: &ReservationKey, id: &str) {
        if let Some(requests) = self.requests.get_mut(key) {
            requests.remove(id);
            if requests.is_empty() {
                self.requests.remove(key);
            }
        }
    }
}

/// Projects active KV allocations in token units, sharing only reusable complete prefixes.
/// Partial prompt tails and generated tokens remain private to each sequence.
pub(crate) fn potential_decode_tokens(
    sequences: &[ActiveSequence],
    request: &RouterRequest,
    block_size: usize,
    assume_kv_reuse: bool,
    track_output_blocks: bool,
) -> usize {
    let mut shared_blocks = BTreeSet::new();
    let mut private_tokens = 0;
    for sequence in sequences {
        let Some(active) = &sequence.request else {
            continue;
        };
        let output_tokens = if track_output_blocks {
            sequence.output_tokens
        } else {
            0
        };
        private_tokens += allocated_tokens(
            &mut shared_blocks,
            active,
            block_size,
            assume_kv_reuse && sequence.prefix_shareable,
            output_tokens,
        );
    }
    if let Some(incoming) = &request.generate_request {
        private_tokens += allocated_tokens(
            &mut shared_blocks,
            incoming,
            block_size,
            assume_kv_reuse && request.reads_prefix_cache(),
            0,
        );
    }
    shared_blocks.len() * block_size + private_tokens
}

/// Adds shareable prefix blocks and returns the sequence's private allocation in tokens.
fn allocated_tokens(
    blocks: &mut BTreeSet<[u8; 32]>,
    request: &vllm_llm::GenerateRequest,
    block_size: usize,
    share_prefix: bool,
    output_tokens: usize,
) -> usize {
    let tokens = &request.prompt_token_ids;
    let total = tokens
        .len()
        .saturating_add(output_tokens)
        .div_ceil(block_size)
        * block_size;
    if !share_prefix {
        return total;
    }
    let identity = serde_json::to_vec(&(
        request
            .cache_salt
            .as_deref()
            .filter(|salt| !salt.is_empty()),
        request.lora_request.as_ref().map(|lora| lora.lora_int_id),
        request
            .mm_features
            .as_ref()
            .map(|_| request.request_id.as_str()),
    ))
    .expect("cache identity contains only strings and integers");
    let mut hash = blake3::Hasher::new();
    hash.update(&(identity.len() as u64).to_le_bytes());
    hash.update(&identity);
    // The final prompt token must be recomputed even on a complete prefix hit.
    let reusable_tokens = tokens.len().saturating_sub(1) / block_size * block_size;
    for block in tokens[..reusable_tokens].chunks_exact(block_size) {
        for token in block {
            hash.update(&token.to_le_bytes());
        }
        blocks.insert(*hash.finalize().as_bytes());
    }
    total - reusable_tokens
}

/// Estimates prompt-token computation from the observation owner's reusable token count.
pub(crate) fn uncached_tokens(
    request: &RouterRequest,
    candidate: &RouteCandidate,
    kv: &dyn KvPrefixIndexer,
) -> usize {
    let Some(info) = crate::cache::cache_match(request, candidate, kv) else {
        return request.token_count();
    };
    info.total_tokens - info.matched_tokens
}
