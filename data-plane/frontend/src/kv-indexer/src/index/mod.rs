// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Source-isolated typed KV locality indexes.
use foretoken_model_protocol::normalized_kv_block_hash;
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
    pub cache_salt: Option<&'a str>,
    pub model_revision: &'a str,
    pub scope_id: &'a str,
    pub hash_format: foretoken_model_protocol::KvHashFormat,
    pub group_idx: Option<u32>,
    pub match_all_groups: bool,
    pub spec_kind: &'a str,
    pub sliding_window: Option<u32>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct KvPrefixMatch {
    pub placement: KvPlacement,
    pub matched_tokens: usize,
}

pub trait KvLocalityIndex: Send {
    /// Applies one source-scoped delta event to index-owned locality facts.
    fn apply(&mut self, source: KvEventSourceId, event: KvIndexEvent, now: Instant);
    /// Removes facts for one exact source epoch and data-parallel rank.
    fn clear_source(&mut self, source: &KvEventSourceId);
    /// Removes facts across all epochs and ranks of one event-source identity.
    fn clear_event_source(&mut self, id: &str);
    /// Refreshes fact liveness for a source after a successful empty delta response.
    fn touch_source(&mut self, source: &KvEventSourceId, now: Instant);
    /// Returns this index's prefix-locality matches without transferring ownership of its facts.
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
    /// Creates the resolved source-local index with the supplied fact retention lifetime.
    ///
    /// `KvIndexer` owns the result and applies source delta events to it for its lifetime.
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
