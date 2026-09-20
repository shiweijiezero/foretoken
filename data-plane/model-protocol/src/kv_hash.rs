// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared keyed identities for privacy-preserving KV events and prefix queries.

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};

use crate::{KvBlockHash, KvPartition};

/// Derives the same opaque block identity for model-server events and frontend queries.
/// The namespace key protects low-entropy tokens and cache salts from disclosure through
/// exported events. A salt enters the first block; descendants inherit it through ancestry.
pub fn normalized_kv_block_hash(
    key: &[u8; 32],
    parent: &KvBlockHash,
    tokens: &[u32],
    partition: &KvPartition,
    cache_salt: Option<&str>,
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
    if parent.0.is_empty()
        && let Some(salt) = cache_salt.filter(|salt| !salt.is_empty())
    {
        hasher.update(b"\0cache_salt\0");
        hasher.update(salt.as_bytes());
    }
    KvBlockHash(URL_SAFE_NO_PAD.encode(hasher.finalize().as_bytes()))
}
