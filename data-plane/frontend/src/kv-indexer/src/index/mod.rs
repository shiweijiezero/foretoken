// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Source-isolated typed KV locality indexes.
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use std::time::{Duration, Instant};

pub mod positional_hash;
pub mod radix_tree;

use crate::{KvLocalityIndexResolvedImplementation, PositionalHashIndex, RadixTreeIndex};
pub use foretoken_model_protocol::{
    KvBlockHash, KvCacheLocality, KvPartition, KvPlacement, KvStorageTier, KvStoredBlock,
};

#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct KvEventSourceId {
    pub event_source_id: String,
    pub model_group_id: String,
    pub epoch: String,
    pub dp_rank: u32,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KvIndexEvent {
    BlockStored {
        blocks: Vec<KvStoredBlock>,
        placement: KvPlacement,
    },
    BlockRemoved {
        block_hashes: Vec<KvBlockHash>,
        placement: KvPlacement,
        group_idx: Option<u32>,
    },
    /// The protocol clear has no selector: it removes every placement and group for this exact
    /// response-envelope source, epoch, and rank.
    AllBlocksCleared,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct KvPrefixQuery<'a> {
    pub tokens: &'a [u32],
    pub model_revision: &'a str,
    pub scope_id: &'a str,
    pub hash_format: foretoken_model_protocol::KvHashFormat,
    pub group_idx: Option<u32>,
    pub spec_kind: &'a str,
    pub sliding_window: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct KvPrefixMatch {
    pub event_source_id: String,
    pub model_group_id: String,
    pub epoch: String,
    pub dp_rank: u32,
    pub placement: KvPlacement,
    pub matched_complete_blocks: u64,
    pub matched_tokens: usize,
    pub last_matched_hash: Option<KvBlockHash>,
}

pub trait KvLocalityIndex: Send {
    fn apply(&mut self, source: KvEventSourceId, event: KvIndexEvent, now: Instant);
    fn clear_source(&mut self, source: &KvEventSourceId);
    fn clear_event_source(&mut self, id: &str);
    fn touch_source(&mut self, source: &KvEventSourceId, now: Instant);
    fn query(
        &mut self,
        source: &KvEventSourceId,
        query: &KvPrefixQuery<'_>,
        key: &[u8; 32],
        now: Instant,
    ) -> Vec<KvPrefixMatch>;
}

pub enum KvLocalityIndexes {
    PositionalHash(PositionalHashIndex),
    RadixTree(RadixTreeIndex),
}

impl KvLocalityIndexes {
    pub fn new(implementation: KvLocalityIndexResolvedImplementation, ttl: Duration) -> Self {
        match implementation {
            KvLocalityIndexResolvedImplementation::PositionalHash => {
                Self::PositionalHash(PositionalHashIndex::new(ttl))
            }
            KvLocalityIndexResolvedImplementation::RadixTree => {
                Self::RadixTree(RadixTreeIndex::new(ttl))
            }
        }
    }
}

impl KvLocalityIndex for KvLocalityIndexes {
    fn apply(&mut self, source: KvEventSourceId, event: KvIndexEvent, now: Instant) {
        match self {
            Self::PositionalHash(index) => index.apply(source, event, now),
            Self::RadixTree(index) => index.apply(source, event, now),
        }
    }

    fn clear_source(&mut self, source: &KvEventSourceId) {
        match self {
            Self::PositionalHash(index) => index.clear_source(source),
            Self::RadixTree(index) => index.clear_source(source),
        }
    }

    fn clear_event_source(&mut self, id: &str) {
        match self {
            Self::PositionalHash(index) => index.clear_event_source(id),
            Self::RadixTree(index) => index.clear_event_source(id),
        }
    }

    fn touch_source(&mut self, source: &KvEventSourceId, now: Instant) {
        match self {
            Self::PositionalHash(index) => index.touch_source(source, now),
            Self::RadixTree(index) => index.touch_source(source, now),
        }
    }

    fn query(
        &mut self,
        source: &KvEventSourceId,
        query: &KvPrefixQuery<'_>,
        key: &[u8; 32],
        now: Instant,
    ) -> Vec<KvPrefixMatch> {
        match self {
            Self::PositionalHash(index) => index.query(source, query, key, now),
            Self::RadixTree(index) => index.query(source, query, key, now),
        }
    }
}

pub(super) fn normalized_block_hash(
    key: &[u8; 32],
    parent: &KvBlockHash,
    tokens: &[u32],
    partition: &KvPartition,
) -> KvBlockHash {
    let mut hasher = blake3::Hasher::new_keyed(key);
    hasher.update(parent.0.as_bytes());
    hasher.update(partition.model_revision.as_bytes());
    hasher.update(partition.scope_id.as_bytes());
    hasher.update(&partition.hash_block_size.to_le_bytes());
    hasher.update(&partition.group_idx.unwrap_or(u32::MAX).to_le_bytes());
    hasher.update(partition.spec_kind.as_bytes());
    hasher.update(&partition.sliding_window.unwrap_or(u32::MAX).to_le_bytes());
    for token in tokens {
        hasher.update(&token.to_le_bytes());
    }
    KvBlockHash(URL_SAFE_NO_PAD.encode(hasher.finalize().as_bytes()))
}
