use std::collections::{BTreeMap, BTreeSet};

use foretoken_model_protocol::{KvDelta, KvDeltaEvent, KvDeltaResponse};
use foretoken_router::{BackendId, KvPrefixScorer, RouteContext};
use vllm_llm::GenerateRequest;

use super::*;
use crate::index::{Delta, KvIndex, Partition, Source, Stored, block_digest, encoded_digest};

struct TestRouteContext {
    request: GenerateRequest,
    capabilities: BTreeSet<String>,
}
impl TestRouteContext {
    fn new(tokens: Vec<u32>) -> Self {
        Self {
            request: GenerateRequest {
                request_id: "request".into(),
                prompt_token_ids: tokens,
                sampling_params: Default::default(),
                mm_features: None,
                arrival_time: None,
                cache_salt: None,
                trace_headers: None,
                priority: 0,
                data_parallel_rank: None,
                reasoning_parser_kwargs: None,
                lora_request: None,
            },
            capabilities: BTreeSet::new(),
        }
    }
    fn context(&self) -> RouteContext<'_> {
        RouteContext {
            model: "model",
            revision: None,
            required_capabilities: &self.capabilities,
            request: &self.request,
        }
    }
}

fn source(id: &str, scope_id: &str) -> KvEventSourceConfig {
    KvEventSourceConfig {
        backend_id: BackendId::new(id),
        endpoint: format!("http://{id}"),
        scope_id: scope_id.into(),
    }
}

fn config() -> KvRuntimeConfig {
    KvRuntimeConfig {
        event_sources: vec![
            source("aggregate", "aggregate-scope"),
            source("prefill", "pd-scope"),
            source("decode", "pd-scope"),
        ],
        route_bindings: BTreeMap::from([
            (
                BackendId::new("aggregate"),
                KvRouteBinding {
                    source_backend_id: BackendId::new("aggregate"),
                },
            ),
            (
                BackendId::new("pd"),
                KvRouteBinding {
                    source_backend_id: BackendId::new("prefill"),
                },
            ),
        ]),
    }
}

fn score(indexer: &KvIndexer, backend_id: &str, tokens: &[u32]) -> usize {
    let request = TestRouteContext::new(tokens.to_vec());
    indexer.score_prefill_prefix(&BackendId::new(backend_id), request.context())
}

fn store_prefix(indexer: &KvIndexer, backend_id: &str, scope_id: &str, tokens: &[u32]) {
    let state = indexer.state.as_ref().unwrap();
    let partition = Partition {
        scope_id: scope_id.into(),
        block_size: tokens.len() as u32,
        group_idx: None,
        spec_kind: "full_attention".into(),
    };
    let digest = encoded_digest(block_digest(&state.key, &[0; 32], tokens, &partition));
    state.index.lock().unwrap().apply(
        Source {
            backend_id: backend_id.into(),
            epoch: "epoch-1".into(),
            dp_rank: 0,
        },
        Delta::Store(Stored { partition, digest }),
        Instant::now(),
    );
}

#[test]
fn digests_and_contiguous_prefix_are_scoped() {
    let key = [7; 32];
    let partition = Partition {
        scope_id: "s".into(),
        block_size: 1,
        group_idx: None,
        spec_kind: "full_attention".into(),
    };
    let source = Source {
        backend_id: "b".into(),
        epoch: "e".into(),
        dp_rank: 0,
    };
    let now = Instant::now();
    let mut index = KvIndex::new(Duration::from_secs(1));
    let first_bytes = block_digest(&key, &[0; 32], &[1], &partition);
    let first = encoded_digest(first_bytes);
    let second = encoded_digest(block_digest(&key, &first_bytes, &[2], &partition));
    index.apply(
        source.clone(),
        Delta::Store(Stored {
            partition: partition.clone(),
            digest: first,
        }),
        now,
    );
    index.apply(
        source.clone(),
        Delta::Store(Stored {
            partition: partition.clone(),
            digest: second,
        }),
        now,
    );
    assert_eq!(index.score(&source, &partition, &[1, 2, 3], &key, now), 2);
    index.clear(&source);
    assert_eq!(index.score(&source, &partition, &[1], &key, now), 0);
}

#[test]
fn ttl_and_partition_isolation() {
    let key = [1; 32];
    let partition = Partition {
        scope_id: "s".into(),
        block_size: 1,
        group_idx: Some(0),
        spec_kind: "a".into(),
    };
    let other = Partition {
        group_idx: Some(1),
        ..partition.clone()
    };
    let source = Source {
        backend_id: "b".into(),
        epoch: "e".into(),
        dp_rank: 0,
    };
    let now = Instant::now();
    let digest = encoded_digest(block_digest(&key, &[0; 32], &[1], &partition));
    let mut index = KvIndex::new(Duration::from_millis(1));
    index.apply(
        source.clone(),
        Delta::Store(Stored {
            partition: partition.clone(),
            digest: digest.clone(),
        }),
        now,
    );
    assert_eq!(index.score(&source, &other, &[1], &key, now), 0);
    assert_eq!(
        index.score(
            &source,
            &partition,
            &[1],
            &key,
            now + Duration::from_secs(1)
        ),
        0
    );
    let mut renewed = KvIndex::new(Duration::from_millis(1));
    renewed.apply(
        source.clone(),
        Delta::Store(Stored {
            partition: partition.clone(),
            digest,
        }),
        now,
    );
    renewed.touch_backend("b", now + Duration::from_secs(1));
    assert_eq!(
        renewed.score(
            &source,
            &partition,
            &[1],
            &key,
            now + Duration::from_secs(1)
        ),
        1
    );
}

#[test]
fn scores_aggregate_and_pd_routes_from_their_prefill_source_only() {
    let indexer = KvIndexer::new(config(), Some([7; 32]));
    store_prefix(&indexer, "aggregate", "aggregate-scope", &[1, 2]);
    assert_eq!(score(&indexer, "aggregate", &[1, 2, 3]), 2);
    store_prefix(&indexer, "decode", "pd-scope", &[1, 2]);
    assert_eq!(score(&indexer, "pd", &[1, 2, 3]), 0);
    store_prefix(&indexer, "prefill", "pd-scope", &[1, 2]);
    assert_eq!(score(&indexer, "pd", &[1, 2, 3]), 2);
}

#[test]
fn missing_key_is_a_neutral_noop_scorer() {
    let indexer = KvIndexer::new(config(), None);
    assert_eq!(score(&indexer, "aggregate", &[1, 2]), 0);
    assert_eq!(indexer.status().state, KvIndexState::Disabled);
}

#[test]
fn credential_failure_is_explicit_but_remains_a_neutral_scorer() {
    let indexer = KvIndexer::degraded(config(), KvIndexDegradedReason::KeyReadFailed);
    assert_eq!(score(&indexer, "aggregate", &[1, 2]), 0);
    assert_eq!(
        indexer.status(),
        KvIndexStatus {
            state: KvIndexState::Degraded,
            reason: Some(KvIndexDegradedReason::KeyReadFailed),
            sources_healthy: 0,
            sources_total: 3,
        }
    );
}

#[test]
fn request_specific_cache_partitions_are_neutral_until_indexed() {
    let indexer = KvIndexer::new(config(), Some([7; 32]));
    store_prefix(&indexer, "aggregate", "aggregate-scope", &[1]);
    let mut request = TestRouteContext::new(vec![1]);
    request.request.cache_salt = Some("tenant-a".into());

    assert_eq!(
        indexer.score_prefill_prefix(&BackendId::new("aggregate"), request.context()),
        0
    );

    request.request.cache_salt = None;
    request.request.sampling_params.skip_reading_prefix_cache = Some(true);
    assert_eq!(
        indexer.score_prefill_prefix(&BackendId::new("aggregate"), request.context()),
        0
    );

    request.request.sampling_params.skip_reading_prefix_cache = None;
    request.request.data_parallel_rank = Some(1);
    assert_eq!(
        indexer.score_prefill_prefix(&BackendId::new("aggregate"), request.context()),
        0
    );
}

#[test]
fn empty_scope_is_a_neutral_prefix_signal() {
    let mut config = config();
    config.event_sources[0].scope_id.clear();
    let indexer = KvIndexer::new(config, Some([7; 32]));
    store_prefix(&indexer, "aggregate", "", &[1]);
    assert_eq!(score(&indexer, "aggregate", &[1]), 0);
}

#[test]
fn delta_identity_and_cursor_are_source_scoped() {
    let indexer = KvIndexer::new(config(), Some([7; 32]));
    let state = indexer.state.as_ref().unwrap();
    let aggregate = &indexer.config.event_sources[0];
    let partition = Partition {
        scope_id: "aggregate-scope".into(),
        block_size: 1,
        group_idx: None,
        spec_kind: "full_attention".into(),
    };
    let digest = encoded_digest(block_digest(&state.key, &[0; 32], &[1], &partition));
    apply_component(
        state,
        aggregate,
        KvDeltaResponse {
            backend_id: "aggregate".into(),
            scope_id: "aggregate-scope".into(),
            epoch: "epoch-1".into(),
            dp_rank: 0,
            through: 1,
            current: 1,
            deltas: vec![KvDelta {
                sequence: 1,
                event: KvDeltaEvent::Store {
                    partition: partition.clone(),
                    digest,
                },
            }],
        },
    )
    .unwrap();
    assert_eq!(score(&indexer, "aggregate", &[1]), 1);
    assert_eq!(
        state.cursors.lock().unwrap()[&BackendId::new("aggregate")].after,
        1
    );
    apply_component(
        state,
        aggregate,
        KvDeltaResponse {
            backend_id: "wrong-backend".into(),
            scope_id: "aggregate-scope".into(),
            epoch: "epoch-1".into(),
            dp_rank: 0,
            through: 2,
            current: 2,
            deltas: vec![],
        },
    )
    .unwrap_err();
    assert_eq!(score(&indexer, "aggregate", &[1]), 0);
    assert!(
        !state
            .cursors
            .lock()
            .unwrap()
            .contains_key(&BackendId::new("aggregate"))
    );
}
