// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Wire-shape contract for normalized vLLM KV lifecycle events.

use foretoken_model_protocol::*;
// Protects the cross-process KV event envelope and privacy-preserving removal payload.
#[test]
fn lifecycle_wire_shape_is_typed_and_hash_only_remove() {
    let placement = KvPlacement {
        tier: KvStorageTier::HostPinned,
        locality: KvCacheLocality::Remote,
    };
    let partition = KvPartition {
        model_revision: "r1".into(),
        scope_id: "tenant".into(),
        hash_format: KvHashFormat::NormalizedKeyedBlake3V1,
        hash_block_size: 2,
        group_idx: Some(3),
        spec_kind: "full".into(),
        sliding_window: None,
    };
    let stored = KvStoredBlock {
        partition,
        block_index: 0,
        parent_hash: KvBlockHash("".into()),
        block_hash: KvBlockHash("normalized".into()),
    };
    let event = KvDeltaEvent::BlockStored {
        blocks: vec![stored],
        placement,
    };
    let json = serde_json::to_value(&event).expect("serialize stored event");
    assert_eq!(json["kind"], "block_stored");
    let delta = KvDelta {
        sequence: 0,
        event: event.clone(),
    };
    let mut nested = serde_json::to_value(&delta).expect("serialize nested delta");
    assert_eq!(nested["event"]["kind"], "block_stored");
    nested["event"]["unexpected"] = serde_json::json!(true);
    assert!(serde_json::from_value::<KvDelta>(nested).is_err());
    assert_eq!(
        serde_json::to_value(ModelServerRole::Prefill).expect("serialize model-server role"),
        serde_json::json!("prefill")
    );
    let remove = KvDeltaEvent::BlockRemoved {
        block_hashes: vec![KvBlockHash("normalized".into())],
        placement,
        group_idx: Some(3),
    };
    assert_eq!(
        serde_json::to_value(&remove).expect("serialize selector-bound hash remove"),
        serde_json::json!({
            "kind": "block_removed",
            "blockHashes": ["normalized"],
            "placement": {"tier": "host_pinned", "locality": "remote"},
            "groupIdx": 3,
        })
    );
    let cleared = serde_json::json!({"kind": "all_blocks_cleared"});
    assert_eq!(
        serde_json::to_value(KvDeltaEvent::AllBlocksCleared).expect("serialize global clear"),
        cleared
    );
    assert_eq!(
        serde_json::from_value::<KvDeltaEvent>(cleared).expect("deserialize selector-free clear"),
        KvDeltaEvent::AllBlocksCleared
    );
    let response = KvDeltaResponse {
        event_source_id: "source".into(),
        model_group_id: "owner".into(),
        epoch: "epoch".into(),
        dp_rank: 0,
        through: 0,
        current: 0,
        deltas: vec![delta],
    };
    let mut envelope = serde_json::to_value(response).expect("serialize delta response");
    envelope["unexpected"] = serde_json::json!(true);
    assert!(serde_json::from_value::<KvDeltaResponse>(envelope).is_err());
    assert_eq!(
        serde_json::to_value(KvCacheLocality::Unspecified).expect("serialize unspecified locality"),
        serde_json::json!("unspecified")
    );
    assert!(
        serde_json::from_value::<KvPlacement>(serde_json::json!({
            "tier": "device"
        }))
        .is_err()
    );
}
