// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::Arc;

use foretoken_model_protocol::{KvCacheLocality, KvDeltaEvent, KvPlacement, KvStorageTier};
use foretoken_model_server::kv_event_adapter::{KvDeltaError, KvEventAdapter};
use serde_json::json;

fn adapter(data_parallel_size: u32) -> Arc<KvEventAdapter> {
    KvEventAdapter::new(
        [7; 32],
        "scope".into(),
        "model-group".into(),
        "r1".into(),
        data_parallel_size,
    )
}

fn batch(event: serde_json::Value, dp_rank: u32) -> Vec<u8> {
    rmp_serde::to_vec(&json!([1.25, [event], dp_rank])).unwrap()
}

fn epoch(adapter: &KvEventAdapter, dp_rank: u32) -> String {
    let KvDeltaError::CursorReset(reset) = adapter.delta(dp_rank, None, None, 2).unwrap_err()
    else {
        panic!("missing epoch must return a cursor reset");
    };
    reset.epoch
}

// Protects rank-local KV replay and prevents raw token or upstream hash disclosure.
#[test]
fn lifecycle_is_rank_local_replayable_and_privacy_preserving() {
    let adapter = adapter(2);
    let stored = json!({
        "type": "BlockStored",
        "block_hashes": [1, 2],
        "token_ids": [1, 2, 3, 4],
        "block_size": 2,
        "medium": "GPU",
        "extra_keys": [null, null],
        "group_idx": 0,
        "kv_cache_spec_kind": "full_attention"
    });
    adapter.ingest_msgpack(1, &batch(stored, 1));

    let other_epoch = epoch(&adapter, 0);
    let epoch = epoch(&adapter, 1);
    let page = adapter.delta(1, Some(&epoch), None, 2).unwrap();
    assert_eq!(page.event_source_id, "model-group:dp:1");
    assert_eq!(page.dp_rank, 1);
    assert_eq!(page.deltas[0].sequence, 0);
    let KvDeltaEvent::BlockStored { blocks, placement } = &page.deltas[0].event else {
        panic!("expected normalized store event");
    };
    assert_eq!(blocks.len(), 2);
    assert_eq!(blocks[0].partition.model_revision, "r1");
    assert_eq!(blocks[0].partition.hash_block_size, 2);
    assert_eq!(blocks[0].block_index, 0);
    assert_eq!(blocks[1].block_index, 1);
    assert_eq!(blocks[1].parent_hash, blocks[0].block_hash);
    assert_eq!(placement.tier, KvStorageTier::Device);
    assert_eq!(placement.locality, KvCacheLocality::Local);
    let encoded = serde_json::to_string(&page).unwrap();
    assert!(!encoded.contains("token_ids") && !encoded.contains("block_hashes\":[1"));
    assert!(
        adapter
            .delta(0, Some(&other_epoch), None, 2)
            .unwrap()
            .deltas
            .is_empty()
    );

    adapter.ingest_msgpack(
        1,
        &batch(json!({"type":"BlockRemoved","block_hashes":[1]}), 1),
    );
    let removed = adapter.delta(1, Some(&epoch), Some(0), 2).unwrap();
    assert_eq!(removed.deltas[0].sequence, 1);
    assert!(matches!(
        removed.deltas[0].event,
        KvDeltaEvent::BlockRemoved { .. }
    ));
}

// Protects local-offload identity, placement normalization, and global clear semantics.
#[test]
fn placement_and_clear_follow_vllm_lifecycle() {
    let adapter = adapter(1);
    adapter.ingest_msgpack(
        0,
        &batch(
            json!({
                "type": "BlockStored",
                "block_hashes": [1],
                "token_ids": [1, 2],
                "block_size": 2,
                "medium": "CPU",
                "locality": "LOCAL",
                "extra_keys": null,
                "kv_cache_spec_kind": "full_attention"
            }),
            0,
        ),
    );
    let epoch = epoch(&adapter, 0);
    assert!(
        adapter
            .delta(0, Some(&epoch), None, 2)
            .unwrap()
            .deltas
            .is_empty()
    );

    adapter.ingest_msgpack(
        0,
        &batch(
            json!({
                "type": "BlockStored",
                "block_hashes": [3],
                "parent_block_hash": 2,
                "token_ids": [3, 4],
                "block_size": 2,
                "medium": "CPU_PINNED",
                "locality": "LOCAL",
                "extra_keys": [null],
                "kv_cache_spec_kind": "full_attention"
            }),
            0,
        ),
    );
    adapter.ingest_msgpack(
        0,
        &batch(
            json!({
                "type": "BlockStored",
                "block_hashes": [2],
                "token_ids": [1, 2],
                "block_size": 2,
                "medium": "CPU_PINNED",
                "locality": "LOCAL",
                "extra_keys": [null],
                "kv_cache_spec_kind": "full_attention"
            }),
            0,
        ),
    );
    let page = adapter.delta(0, Some(&epoch), None, 2).unwrap();
    assert_eq!(page.deltas.len(), 2);
    for delta in &page.deltas {
        assert!(matches!(
            delta.event,
            KvDeltaEvent::BlockStored {
                placement: KvPlacement {
                    tier: KvStorageTier::HostPinned,
                    locality: KvCacheLocality::Local
                },
                ..
            }
        ));
    }
    let KvDeltaEvent::BlockStored { blocks: root, .. } = &page.deltas[0].event else {
        unreachable!()
    };
    let KvDeltaEvent::BlockStored { blocks: child, .. } = &page.deltas[1].event else {
        unreachable!()
    };
    assert_eq!(root[0].block_index, 0);
    assert_eq!(child[0].block_index, 1);
    assert_eq!(child[0].parent_hash, root[0].block_hash);

    adapter.ingest_msgpack(0, &batch(json!({"type":"AllBlocksCleared"}), 0));
    let clear = adapter.delta(0, Some(&epoch), Some(1), 2).unwrap();
    assert!(matches!(
        clear.deltas[0].event,
        KvDeltaEvent::AllBlocksCleared
    ));
}

// A corrupt publisher must invalidate only its own rank, including when it claims another rank.
#[test]
fn protocol_failure_is_unavailable_and_a_valid_batch_recovers() {
    let adapter = adapter(2);
    let other_epoch = epoch(&adapter, 1);
    let epoch = epoch(&adapter, 0);
    adapter.ingest_msgpack(0, b"not-msgpack");
    assert!(matches!(
        adapter.delta(0, Some(&epoch), None, 2),
        Err(KvDeltaError::Unavailable)
    ));

    adapter.ingest_msgpack(0, &batch(json!({"type":"AllBlocksCleared"}), 0));
    let KvDeltaError::CursorReset(reset) = adapter.delta(0, Some(&epoch), None, 2).unwrap_err()
    else {
        panic!("recovered rank must invalidate the old source epoch");
    };
    assert!(adapter.delta(0, Some(&reset.epoch), None, 2).is_ok());
    adapter.ingest_msgpack(0, &batch(json!({"type":"AllBlocksCleared"}), 1));
    assert!(matches!(
        adapter.delta(0, Some(&epoch), None, 2),
        Err(KvDeltaError::Unavailable)
    ));
    assert!(adapter.delta(1, Some(&other_epoch), None, 2).is_ok());
}

// Protects cursor reset responses with the exact source and rank identity.
#[test]
fn invalid_cursor_returns_current_source_identity() {
    let adapter = adapter(2);
    let epoch = epoch(&adapter, 1);
    let KvDeltaError::CursorReset(reset) = adapter.delta(1, Some(&epoch), Some(10), 2).unwrap_err()
    else {
        panic!("cursor reset must not be reported as adapter unavailability");
    };
    assert_eq!(reset.event_source_id, "model-group:dp:1");
    assert_eq!(reset.dp_rank, 1);
    assert_eq!(reset.through, 0);
    assert_eq!(reset.current, 0);
}
