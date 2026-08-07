// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//! Pure KV digest and soft-prefix index operations.

use std::collections::HashMap;
use std::time::{Duration, Instant};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use foretoken_model_protocol::KvPartition;
use serde::{Deserialize, Serialize};

pub use foretoken_model_protocol::KvPartition as Partition;

#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct Source {
    pub backend_id: String,
    pub epoch: String,
    pub dp_rank: u32,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Stored {
    pub partition: KvPartition,
    pub digest: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Delta {
    Store(Stored),
    Remove(Stored),
    Clear,
}

/// Matches model-server: raw 32-byte parent digest followed by one full block.
pub fn block_digest(
    key: &[u8; 32],
    parent: &[u8; 32],
    tokens: &[u32],
    partition: &KvPartition,
) -> [u8; 32] {
    let mut hasher = blake3::Hasher::new_keyed(key);
    hasher.update(parent);
    hasher.update(partition.scope_id.as_bytes());
    hasher.update(&partition.block_size.to_le_bytes());
    hasher.update(&partition.group_idx.unwrap_or(u32::MAX).to_le_bytes());
    hasher.update(partition.spec_kind.as_bytes());
    for token in tokens {
        hasher.update(&token.to_le_bytes());
    }
    *hasher.finalize().as_bytes()
}

pub(crate) fn encoded_digest(digest: [u8; 32]) -> String {
    URL_SAFE_NO_PAD.encode(digest)
}

pub struct KvIndex {
    ttl: Duration,
    credits: HashMap<Source, HashMap<(KvPartition, String), Instant>>,
}

impl KvIndex {
    pub fn new(ttl: Duration) -> Self {
        Self {
            ttl,
            credits: HashMap::new(),
        }
    }

    pub fn apply(&mut self, source: Source, delta: Delta, now: Instant) {
        match delta {
            Delta::Clear => {
                self.credits.remove(&source);
            }
            Delta::Store(item) => {
                self.credits
                    .entry(source)
                    .or_default()
                    .insert((item.partition, item.digest), now);
            }
            Delta::Remove(item) => {
                if let Some(entries) = self.credits.get_mut(&source) {
                    entries.remove(&(item.partition, item.digest));
                }
            }
        }
    }

    pub fn clear(&mut self, source: &Source) {
        self.credits.remove(source);
    }

    pub fn score(
        &mut self,
        source: &Source,
        partition: &KvPartition,
        tokens: &[u32],
        key: &[u8; 32],
        now: Instant,
    ) -> usize {
        let Some(entries) = self.credits.get_mut(source) else {
            return 0;
        };
        entries.retain(|_, at| now.duration_since(*at) <= self.ttl);
        if partition.block_size == 0 {
            return 0;
        }
        let mut parent = [0; 32];
        let mut score = 0;
        for block in tokens.chunks_exact(partition.block_size as usize) {
            let digest = block_digest(key, &parent, block, partition);
            if !entries.contains_key(&(partition.clone(), encoded_digest(digest))) {
                break;
            }
            parent = digest;
            score += block.len();
        }
        score
    }

    pub fn clear_backend(&mut self, backend_id: &str) {
        self.credits
            .retain(|source, _| source.backend_id != backend_id);
    }

    /// Renews precise credits while their source remains healthy and continuously observed.
    pub fn touch_backend(&mut self, backend_id: &str, now: Instant) {
        for (source, entries) in &mut self.credits {
            if source.backend_id == backend_id {
                entries.values_mut().for_each(|at| *at = now);
            }
        }
    }

    /// Scores one component across its current epoch without retaining prompt tokens.
    pub fn score_backend(
        &mut self,
        backend_id: &str,
        scope_id: &str,
        tokens: &[u32],
        key: &[u8; 32],
        now: Instant,
    ) -> usize {
        let partitions = self
            .credits
            .iter()
            .filter_map(|(source, entries)| {
                (source.backend_id == backend_id).then_some(
                    entries
                        .keys()
                        .map(|(partition, _)| partition.clone())
                        .collect::<Vec<_>>(),
                )
            })
            .flatten()
            .filter(|partition| partition.scope_id == scope_id)
            .collect::<std::collections::HashSet<_>>();
        let sources = self
            .credits
            .keys()
            .filter(|source| source.backend_id == backend_id)
            .cloned()
            .collect::<Vec<_>>();
        let mut best = 0;
        for source in sources {
            for partition in &partitions {
                best = best.max(self.score(&source, partition, tokens, key, now));
            }
        }
        best
    }
}
