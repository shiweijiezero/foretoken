use super::*;
use foretoken_model_protocol::KvDeltaEvent as DeltaKind;
use serde_json::json;
fn a() -> Arc<KvEventAdapter> {
    KvEventAdapter::new([7; 32], "scope".into(), "backend".into())
}
fn batch(e: serde_json::Value) -> Vec<u8> {
    rmp_serde::to_vec(&json!([1.25, [e], 0])).unwrap()
}
#[test]
fn strict_wire_and_privacy() {
    let x = a();
    let e = json!({"type":"BlockStored","block_hashes":[1],"token_ids":[1,2],"block_size":2,"medium":"GPU","extra_keys":null,"group_idx":0,"kv_cache_spec_kind":"full_attention"});
    x.ingest_frames(vec![TOPIC.to_vec(), 0u64.to_be_bytes().to_vec(), batch(e)]);
    let ep = x.inner.lock().unwrap().epoch.clone();
    let stored = x.delta(&ep, 0, 2).unwrap();
    let out = serde_json::to_string(&stored).unwrap();
    assert!(
        matches!(stored.deltas[0].event, DeltaKind::Store { .. }),
        "deltas = {:?}",
        stored.deltas
    );
    assert!(!out.contains("token_ids") && !out.contains("block_hashes"));

    x.ingest_frames(vec![
        TOPIC.to_vec(),
        1u64.to_be_bytes().to_vec(),
        batch(json!({"type":"BlockRemoved","block_hashes":[1]})),
    ]);
    let removed = x.delta(&ep, 1, 2).unwrap();
    assert!(matches!(removed.deltas[0].event, DeltaKind::Remove { .. }));

    x.ingest_frames(vec![
        TOPIC.to_vec(),
        3u64.to_be_bytes().to_vec(),
        batch(json!({"type":"AllBlocksCleared"})),
    ]);
    assert!(matches!(x.delta(&ep, 2, 2), Err(KvDeltaError::Unavailable)))
}

#[test]
fn protocol_failure_is_reported_as_unavailable_and_recovers_on_a_valid_batch() {
    let x = a();
    let epoch = x.inner.lock().unwrap().epoch.clone();
    x.ingest_msgpack(b"not-msgpack");
    assert!(matches!(
        x.delta(&epoch, 0, 2),
        Err(KvDeltaError::Unavailable)
    ));

    x.ingest_msgpack(&batch(json!({"type":"AllBlocksCleared"})));
    assert!(x.delta(&epoch, 0, 2).is_ok());
}

#[test]
fn rejects_a_cursor_ahead_of_the_current_epoch() {
    let x = a();
    let epoch = x.inner.lock().unwrap().epoch.clone();
    let KvDeltaError::CursorReset(reset) = x.delta(&epoch, 10, 2).unwrap_err() else {
        panic!("cursor reset must not be reported as adapter unavailability");
    };
    assert_eq!(reset.through, 0);
    assert_eq!(reset.current, 0);
}
