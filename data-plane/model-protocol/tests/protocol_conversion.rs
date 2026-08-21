// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Wire-shape contract for normalized vLLM KV lifecycle events.

use foretoken_model_protocol::*;
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

#[test]
fn kv_delta_query_requires_camel_case_data_parallel_rank() {
    let query: KvDeltaQuery =
        serde_json::from_str(r#"{"dpRank":1,"limit":16}"#).expect("camel-case query");
    assert_eq!(query.dp_rank, 1);
    assert!(serde_json::from_str::<KvDeltaQuery>(r#"{"limit":16}"#).is_err());
    assert!(serde_json::from_str::<KvDeltaQuery>(r#"{"dp_rank":1}"#).is_err());
}

#[test]
fn generate_input_preserves_session_id_across_vllm_conversions() {
    let input = GenerateInput {
        request_id: "request-1".into(),
        prompt_token_ids: vec![1, 2],
        sampling_params: Default::default(),
        mm_features: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: Some(3),
        session_id: Some("session-1".into()),
        reasoning_parser_kwargs: None,
        lora_request: None,
    };

    let vllm_request = vllm_llm::GenerateRequest::from(input);
    assert_eq!(vllm_request.session_id.as_deref(), Some("session-1"));

    let round_trip = GenerateInput::from(vllm_request);
    assert_eq!(round_trip.session_id.as_deref(), Some("session-1"));
}

#[test]
fn generate_input_defaults_session_id_for_older_payloads() {
    let input: GenerateInput = serde_json::from_value(serde_json::json!({
        "request_id": "request-1",
        "prompt_token_ids": [1, 2],
        "sampling_params": {},
    }))
    .expect("payload without session_id remains valid");

    assert_eq!(input.session_id, None);
}
