// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Flat complete-block index with explicit ancestry traversal for branch invalidation.
use super::*;
use std::{
    collections::{BTreeMap, BTreeSet},
    time::{Duration, Instant},
};

#[derive(Clone)]
struct Entry {
    block: KvStoredBlock,
    placement: KvPlacement,
    observed_at: Instant,
}

pub struct PositionalHashIndex {
    ttl: Duration,
    entries_by_source: BTreeMap<KvEventSourceId, Vec<Entry>>,
}

impl PositionalHashIndex {
    /// Creates an empty complete-block index that expires observations after `ttl`.
    ///
    /// `KvLocalityIndexes` owns the index and feeds it source-scoped delta events.
    pub fn new(ttl: Duration) -> Self {
        Self {
            ttl,
            entries_by_source: BTreeMap::new(),
        }
    }

    // Computes the transitive descendants of removed blocks so a branch invalidation cannot leave
    // descendants whose stored ancestry is no longer valid.
    fn descendants(entries: &[Entry], roots: &BTreeSet<KvBlockHash>) -> BTreeSet<KvBlockHash> {
        let mut removed = roots.clone();
        loop {
            let descendants = entries
                .iter()
                .filter(|entry| removed.contains(&entry.block.parent_hash))
                .map(|entry| entry.block.block_hash.clone())
                .collect::<BTreeSet<_>>();
            let removed_count = removed.len();
            removed.extend(descendants);
            if removed.len() == removed_count {
                return removed;
            }
        }
    }

    fn remove_branch(entries: &mut Vec<Entry>, placement: KvPlacement, root: &KvBlockHash) {
        let roots = BTreeSet::from([root.clone()]);
        let removed = Self::descendants(entries, &roots);
        entries.retain(|entry| {
            entry.placement != placement || !removed.contains(&entry.block.block_hash)
        });
    }

    // Expires stale roots together with their descendants, preserving valid ancestry in each source.
    fn prune(&mut self, now: Instant) {
        self.entries_by_source.retain(|_, entries| {
            let expired = entries
                .iter()
                .filter(|entry| now.saturating_duration_since(entry.observed_at) > self.ttl)
                .map(|entry| (entry.placement, entry.block.block_hash.clone()))
                .collect::<Vec<_>>();
            for (placement, hash) in expired {
                Self::remove_branch(entries, placement, &hash);
            }
            !entries.is_empty()
        });
    }

    fn matching_partitions(entries: &[Entry], query: &KvPrefixQuery<'_>) -> BTreeSet<KvPartition> {
        entries
            .iter()
            .map(|entry| &entry.block.partition)
            .filter(|partition| {
                partition.model_revision == query.model_revision
                    && partition.scope_id == query.scope_id
                    && partition.hash_format == query.hash_format
                    && (query.match_all_groups || partition.group_idx == query.group_idx)
                    && partition.spec_kind == query.spec_kind
                    && partition.sliding_window == query.sliding_window
                    && partition.hash_block_size > 0
            })
            .cloned()
            .collect()
    }

    fn matching(
        entries: &[Entry],
        query: &KvPrefixQuery<'_>,
        key: &[u8; 32],
    ) -> Vec<KvPrefixMatch> {
        let placements = entries
            .iter()
            .map(|entry| entry.placement)
            .filter(|placement| {
                placement.locality != foretoken_model_protocol::KvCacheLocality::Unspecified
            })
            .collect::<BTreeSet<_>>();
        let partitions = Self::matching_partitions(entries, query);
        let groups = if query.match_all_groups {
            entries
                .iter()
                .map(|entry| &entry.block.partition)
                .filter(|partition| {
                    partition.model_revision == query.model_revision
                        && partition.scope_id == query.scope_id
                        && partition.hash_format == query.hash_format
                })
                .map(|partition| partition.group_idx)
                .collect::<BTreeSet<_>>()
        } else {
            partitions
                .iter()
                .map(|partition| partition.group_idx)
                .collect()
        };
        let mut matches = Vec::new();

        for placement in placements {
            let mut matched_by_group = BTreeMap::<Option<u32>, usize>::new();
            for partition in &partitions {
                let mut parent = KvBlockHash(String::new());
                let mut matched_complete_blocks = 0;
                for (block_index, tokens) in query
                    .tokens
                    .chunks_exact(partition.hash_block_size as usize)
                    .enumerate()
                {
                    let hash =
                        normalized_kv_block_hash(key, &parent, tokens, partition, query.cache_salt);
                    let found = entries.iter().any(|entry| {
                        entry.placement == placement
                            && entry.block.partition == *partition
                            && entry.block.block_index == block_index as u64
                            && entry.block.parent_hash == parent
                            && entry.block.block_hash == hash
                    });
                    if !found {
                        break;
                    }
                    parent = hash;
                    matched_complete_blocks += 1;
                }
                let matched_tokens =
                    matched_complete_blocks as usize * partition.hash_block_size as usize;
                matched_by_group
                    .entry(partition.group_idx)
                    .and_modify(|current| *current = (*current).max(matched_tokens))
                    .or_insert(matched_tokens);
            }
            let matched_tokens = if query.match_all_groups {
                (matched_by_group.len() == groups.len())
                    .then(|| matched_by_group.values().copied().min())
                    .flatten()
                    .unwrap_or(0)
            } else {
                matched_by_group.values().copied().max().unwrap_or(0)
            };
            if matched_tokens > 0 {
                matches.push(KvPrefixMatch {
                    placement,
                    matched_tokens,
                });
            }
        }
        matches.sort();
        matches
    }

    fn clear_group(entries: &mut Vec<Entry>, placement: KvPlacement, group_idx: Option<u32>) {
        entries.retain(|entry| {
            entry.placement != placement || entry.block.partition.group_idx != group_idx
        });
    }
}

impl KvLocalityIndex for PositionalHashIndex {
    fn block_size(&self, source: &KvEventSourceId, query: &KvPrefixQuery<'_>) -> Option<u32> {
        let entries = self.entries_by_source.get(source)?;
        let sizes = Self::matching_partitions(entries, query)
            .into_iter()
            .map(|partition| partition.hash_block_size)
            .collect::<BTreeSet<_>>();
        (sizes.len() == 1).then(|| *sizes.first().unwrap())
    }

    fn apply(&mut self, source: KvEventSourceId, event: KvIndexEvent, now: Instant) {
        match event {
            KvIndexEvent::BlockStored { blocks, placement } => {
                let entries = self.entries_by_source.entry(source).or_default();
                for block in blocks {
                    if let Some(entry) = entries.iter_mut().find(|entry| {
                        entry.placement == placement && entry.block.block_hash == block.block_hash
                    }) {
                        entry.observed_at = now;
                    } else {
                        entries.push(Entry {
                            block,
                            placement,
                            observed_at: now,
                        });
                    }
                }
            }
            KvIndexEvent::BlockRemoved {
                block_hashes,
                placement,
                group_idx,
            } => {
                let Some(entries) = self.entries_by_source.get_mut(&source) else {
                    return;
                };
                let roots = block_hashes
                    .iter()
                    .filter(|hash| {
                        entries.iter().any(|entry| {
                            entry.placement == placement
                                && entry.block.partition.group_idx == group_idx
                                && entry.block.block_hash == **hash
                        })
                    })
                    .cloned()
                    .collect::<BTreeSet<_>>();
                if roots.len() != block_hashes.len() {
                    Self::clear_group(entries, placement, group_idx);
                } else {
                    for root in roots {
                        Self::remove_branch(entries, placement, &root);
                    }
                }
            }
            // A protocol clear is selector-free, so only the exact source envelope scopes it.
            KvIndexEvent::AllBlocksCleared => {
                self.clear_source(&source);
            }
        }
    }

    fn clear_source(&mut self, source: &KvEventSourceId) {
        self.entries_by_source.remove(source);
    }

    fn clear_event_source(&mut self, id: &str) {
        self.entries_by_source
            .retain(|source, _| source.event_source_id != id);
    }

    fn touch_source(&mut self, source: &KvEventSourceId, now: Instant) {
        if let Some(entries) = self.entries_by_source.get_mut(source) {
            for entry in entries {
                entry.observed_at = now;
            }
        }
    }

    fn query(
        &mut self,
        source: &KvEventSourceId,
        query: &KvPrefixQuery<'_>,
        key: &[u8; 32],
        now: Instant,
    ) -> Vec<KvPrefixMatch> {
        self.prune(now);
        self.entries_by_source
            .get(source)
            .map(|entries| Self::matching(entries, query, key))
            .unwrap_or_default()
    }
}
