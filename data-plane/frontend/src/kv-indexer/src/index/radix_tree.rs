// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Source-scoped Patricia trees with reverse metadata and a thin out-of-order adapter.
use super::*;
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use patricia_tree::PatriciaMap;
use std::{
    collections::{BTreeMap, BTreeSet},
    time::{Duration, Instant},
};

const HASH_BYTES: usize = 32;

struct Entry {
    block: KvStoredBlock,
    observed_at: Instant,
}

struct PendingBlock {
    block: KvStoredBlock,
    observed_at: Instant,
}

#[derive(Default)]
struct GroupTree {
    entries_by_path: PatriciaMap<Entry>,
    paths_by_hash: BTreeMap<KvBlockHash, Vec<u8>>,
    pending_by_parent: BTreeMap<KvBlockHash, BTreeMap<KvBlockHash, PendingBlock>>,
}

#[derive(Default)]
struct PlacementTree {
    trees_by_group: BTreeMap<Option<u32>, GroupTree>,
}

#[derive(Default)]
struct SourceTree {
    trees_by_placement: BTreeMap<KvPlacement, PlacementTree>,
}

pub struct RadixTreeIndex {
    ttl: Duration,
    trees_by_source: BTreeMap<KvEventSourceId, SourceTree>,
}

impl RadixTreeIndex {
    pub fn new(ttl: Duration) -> Self {
        Self {
            ttl,
            trees_by_source: BTreeMap::new(),
        }
    }

    fn hash_bytes(hash: &KvBlockHash) -> Option<Vec<u8>> {
        let bytes = URL_SAFE_NO_PAD.decode(&hash.0).ok()?;
        (bytes.len() == HASH_BYTES).then_some(bytes)
    }

    fn pending_parent(tree: &GroupTree, child: &KvBlockHash) -> Option<KvBlockHash> {
        tree.pending_by_parent
            .iter()
            .find_map(|(parent, children)| children.contains_key(child).then(|| parent.clone()))
    }

    fn would_form_pending_cycle(tree: &GroupTree, block: &KvStoredBlock) -> bool {
        if block.parent_hash == block.block_hash {
            return true;
        }
        let mut parent = block.parent_hash.clone();
        let mut visited = BTreeSet::new();
        while !parent.0.is_empty() && visited.insert(parent.clone()) {
            if parent == block.block_hash {
                return true;
            }
            let Some(next) = Self::pending_parent(tree, &parent) else {
                return false;
            };
            parent = next;
        }
        !parent.0.is_empty()
    }

    fn store_block(tree: &mut GroupTree, block: KvStoredBlock, now: Instant) {
        if let Some(path) = tree.paths_by_hash.get(&block.block_hash) {
            if let Some(entry) = tree.entries_by_path.get_mut(path) {
                entry.observed_at = now;
            }
            return;
        }
        let block_bytes = match Self::hash_bytes(&block.block_hash) {
            Some(bytes) => bytes,
            None => return,
        };
        let mut path = if block.parent_hash.0.is_empty() {
            Vec::new()
        } else if let Some(parent_path) = tree.paths_by_hash.get(&block.parent_hash) {
            parent_path.clone()
        } else {
            if Self::would_form_pending_cycle(tree, &block) {
                *tree = GroupTree::default();
                return;
            }
            tree.pending_by_parent
                .entry(block.parent_hash.clone())
                .or_default()
                .insert(
                    block.block_hash.clone(),
                    PendingBlock {
                        block,
                        observed_at: now,
                    },
                );
            return;
        };
        path.extend(block_bytes);
        tree.paths_by_hash
            .insert(block.block_hash.clone(), path.clone());
        tree.entries_by_path.insert(
            path,
            Entry {
                block: block.clone(),
                observed_at: now,
            },
        );
        if let Some(children) = tree.pending_by_parent.remove(&block.block_hash) {
            for child in children.into_values() {
                Self::store_block(tree, child.block, child.observed_at);
            }
        }
    }

    fn remove_subtree(tree: &mut GroupTree, root: &KvBlockHash) {
        let Some(path) = tree.paths_by_hash.get(root).cloned() else {
            return;
        };
        // Patricia split is byte-oriented; paths are only created by concatenating whole hashes.
        let removed = tree.entries_by_path.split_by_prefix(&path);
        for (_, entry) in removed.iter() {
            tree.paths_by_hash.remove(&entry.block.block_hash);
            tree.pending_by_parent.remove(&entry.block.block_hash);
        }
        tree.pending_by_parent.remove(root);
    }

    fn prune(&mut self, now: Instant) {
        self.trees_by_source.retain(|_, source_tree| {
            source_tree.trees_by_placement.retain(|_, placement_tree| {
                placement_tree.trees_by_group.retain(|_, tree| {
                    let expired = tree
                        .entries_by_path
                        .iter()
                        .filter(|(_, entry)| {
                            now.saturating_duration_since(entry.observed_at) > self.ttl
                        })
                        .map(|(_, entry)| entry.block.block_hash.clone())
                        .collect::<Vec<_>>();
                    for hash in expired {
                        Self::remove_subtree(tree, &hash);
                    }
                    tree.pending_by_parent.retain(|_, children| {
                        children.retain(|_, pending| {
                            now.saturating_duration_since(pending.observed_at) <= self.ttl
                        });
                        !children.is_empty()
                    });
                    // Fresh pending metadata must survive for a late parent; expired pending was
                    // removed above, so it cannot retain a source/placement/group indefinitely.
                    !tree.entries_by_path.is_empty() || !tree.pending_by_parent.is_empty()
                });
                !placement_tree.trees_by_group.is_empty()
            });
            !source_tree.trees_by_placement.is_empty()
        });
    }

    fn matching_partitions(tree: &GroupTree, query: &KvPrefixQuery<'_>) -> BTreeSet<KvPartition> {
        tree.entries_by_path
            .iter()
            .map(|(_, entry)| &entry.block.partition)
            .filter(|partition| {
                partition.model_revision == query.model_revision
                    && partition.scope_id == query.scope_id
                    && partition.hash_format == query.hash_format
                    && partition.group_idx == query.group_idx
                    && partition.spec_kind == query.spec_kind
                    && partition.sliding_window == query.sliding_window
                    && partition.hash_block_size > 0
            })
            .cloned()
            .collect()
    }

    fn query_path(key: &[u8; 32], query: &KvPrefixQuery<'_>, partition: &KvPartition) -> Vec<u8> {
        let mut parent = KvBlockHash(String::new());
        let mut path = Vec::new();
        for tokens in query
            .tokens
            .chunks_exact(partition.hash_block_size as usize)
        {
            let hash = normalized_block_hash(key, &parent, tokens, partition);
            let Some(bytes) = Self::hash_bytes(&hash) else {
                return Vec::new();
            };
            path.extend(bytes);
            parent = hash;
        }
        path
    }

    fn matching(
        source: &KvEventSourceId,
        source_tree: &SourceTree,
        query: &KvPrefixQuery<'_>,
        key: &[u8; 32],
    ) -> Vec<KvPrefixMatch> {
        let mut matches = Vec::new();
        for (placement, placement_tree) in &source_tree.trees_by_placement {
            if placement.locality == foretoken_model_protocol::KvCacheLocality::Unspecified {
                continue;
            }
            let Some(tree) = placement_tree.trees_by_group.get(&query.group_idx) else {
                continue;
            };
            for partition in Self::matching_partitions(tree, query) {
                let query_path = Self::query_path(key, query, &partition);
                // `common_prefixes` performs the compressed-trie lookup; direct boundary probes
                // then select the full-block prefix even when the compressed iterator coalesces it.
                let mut best = tree
                    .entries_by_path
                    .common_prefixes(&query_path)
                    .filter(|(path, entry)| {
                        path.len() % HASH_BYTES == 0 && entry.block.partition == partition
                    })
                    .map(|(path, entry)| (path.len(), entry))
                    .max_by_key(|(length, _)| *length);
                for end in (HASH_BYTES..=query_path.len()).step_by(HASH_BYTES) {
                    if let Some(entry) = tree.entries_by_path.get(&query_path[..end])
                        && entry.block.partition == partition
                    {
                        best = Some((end, entry));
                    }
                }
                let Some((length, entry)) = best else {
                    continue;
                };
                let matched_complete_blocks = (length / HASH_BYTES) as u64;
                matches.push(KvPrefixMatch {
                    event_source_id: source.event_source_id.clone(),
                    model_group_id: source.model_group_id.clone(),
                    epoch: source.epoch.clone(),
                    dp_rank: source.dp_rank,
                    placement: *placement,
                    matched_complete_blocks,
                    matched_tokens: matched_complete_blocks as usize
                        * partition.hash_block_size as usize,
                    last_matched_hash: Some(entry.block.block_hash.clone()),
                });
            }
        }
        matches.sort();
        matches
    }
}

impl KvLocalityIndex for RadixTreeIndex {
    fn apply(&mut self, source: KvEventSourceId, event: KvIndexEvent, now: Instant) {
        match event {
            KvIndexEvent::BlockStored { blocks, placement } => {
                let placement_tree = self
                    .trees_by_source
                    .entry(source)
                    .or_default()
                    .trees_by_placement
                    .entry(placement)
                    .or_default();
                for block in blocks {
                    Self::store_block(
                        placement_tree
                            .trees_by_group
                            .entry(block.partition.group_idx)
                            .or_default(),
                        block,
                        now,
                    );
                }
            }
            KvIndexEvent::BlockRemoved {
                block_hashes,
                placement,
                group_idx,
            } => {
                let Some(tree) = self
                    .trees_by_source
                    .get_mut(&source)
                    .and_then(|source_tree| source_tree.trees_by_placement.get_mut(&placement))
                    .and_then(|placement_tree| placement_tree.trees_by_group.get_mut(&group_idx))
                else {
                    return;
                };
                if block_hashes
                    .iter()
                    .all(|hash| tree.paths_by_hash.contains_key(hash))
                {
                    for hash in block_hashes {
                        Self::remove_subtree(tree, &hash);
                    }
                } else {
                    // A hash-only remove without reverse metadata cannot identify a safe subtree.
                    *tree = GroupTree::default();
                }
            }
            // A protocol clear is selector-free, so only the exact source envelope scopes it.
            KvIndexEvent::AllBlocksCleared => self.clear_source(&source),
        }
    }

    fn clear_source(&mut self, source: &KvEventSourceId) {
        self.trees_by_source.remove(source);
    }

    fn clear_event_source(&mut self, id: &str) {
        self.trees_by_source
            .retain(|source, _| source.event_source_id != id);
    }

    fn touch_source(&mut self, source: &KvEventSourceId, now: Instant) {
        let Some(source_tree) = self.trees_by_source.get_mut(source) else {
            return;
        };
        for placement_tree in source_tree.trees_by_placement.values_mut() {
            for tree in placement_tree.trees_by_group.values_mut() {
                for (_, entry) in tree.entries_by_path.iter_mut() {
                    entry.observed_at = now;
                }
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
        self.trees_by_source
            .get(source)
            .map(|source_tree| Self::matching(source, source_tree, query, key))
            .unwrap_or_default()
    }
}
