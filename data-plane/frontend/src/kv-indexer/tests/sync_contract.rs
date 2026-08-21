// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Public route binding, status, and source cursor contracts.

use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

use axum::{Json, Router, extract::State, response::IntoResponse, routing::get};
use foretoken_kv_indexer::{
    KvAutoResolutionReason, KvEventSourceConfig, KvIndexDegradedReason, KvIndexState, KvIndexer,
    KvLocalityIndexImplementation, KvPrefixIndexer, KvPrefixLookup, KvPrefixQueryResult,
    KvPrefixUnavailableReason, KvRouteBinding, KvRuntimeConfig, KvRuntimeConfigError,
};
use foretoken_model_protocol::{KV_INDEX_DELTA_PATH, KvDelta, KvDeltaEvent, KvDeltaResponse};
use tokio::sync::Mutex;

fn source(endpoint: String) -> KvEventSourceConfig {
    KvEventSourceConfig {
        event_source_id: "source-a".into(),
        model_group_id: "owner-a".into(),
        endpoint,
        dp_rank: 0,
        model_revision: "revision".into(),
        scope_id: "scope".into(),
        spec_kind: "full".into(),
        sliding_window: None,
        group_idx: None,
    }
}

fn runtime(endpoint: String) -> KvRuntimeConfig {
    KvRuntimeConfig {
        event_sources: vec![source(endpoint)],
        route_bindings: BTreeMap::from([(
            "route-not-owner".into(),
            KvRouteBinding {
                data_parallel_rank_event_source_ids: BTreeMap::from([(0, "source-a".into())]),
                readable_placements: Default::default(),
                can_restore_or_transfer: false,
            },
        )]),
        requested_implementation: KvLocalityIndexImplementation::Auto,
    }
}

fn page(sequence: u64, epoch: &str) -> KvDeltaResponse {
    pages(&[sequence], epoch)
}

fn pages(sequences: &[u64], epoch: &str) -> KvDeltaResponse {
    KvDeltaResponse {
        event_source_id: "source-a".into(),
        model_group_id: "owner-a".into(),
        epoch: epoch.into(),
        dp_rank: 0,
        through: *sequences.last().expect("test pages contain a delta"),
        current: *sequences.iter().max().expect("test pages contain a delta"),
        deltas: sequences
            .iter()
            .map(|sequence| KvDelta {
                sequence: *sequence,
                event: KvDeltaEvent::AllBlocksCleared,
            })
            .collect(),
    }
}

async fn endpoint(pages: Vec<KvDeltaResponse>) -> (String, tokio::task::JoinHandle<()>) {
    async fn delta(State(pages): State<Arc<Mutex<Vec<KvDeltaResponse>>>>) -> impl IntoResponse {
        Json(pages.lock().await.remove(0))
    }

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind local test server");
    let address = listener.local_addr().expect("read local test address");
    let app = Router::new()
        .route(KV_INDEX_DELTA_PATH, get(delta))
        .with_state(Arc::new(Mutex::new(pages)));
    let task = tokio::spawn(async move {
        axum::serve(listener, app)
            .await
            .expect("serve local test responses");
    });
    (format!("http://{address}"), task)
}

#[test]
fn route_binding_is_exact_and_auto_status_reports_observed_topology() {
    let config = runtime("http://unused".into());
    let indexer = KvIndexer::new(config, Some([7; 32])).expect("valid route binding");
    let status = indexer.status();
    assert_eq!(status.state, KvIndexState::Starting);
    assert_eq!(
        status.auto_resolution_reason,
        Some(KvAutoResolutionReason::SingleSourcePrimaryRank)
    );

    let invalid = KvRuntimeConfig {
        event_sources: vec![source("http://unused".into())],
        route_bindings: BTreeMap::from([(
            "route".into(),
            KvRouteBinding {
                data_parallel_rank_event_source_ids: BTreeMap::from([(0, "missing".into())]),
                readable_placements: Default::default(),
                can_restore_or_transfer: false,
            },
        )]),
        requested_implementation: KvLocalityIndexImplementation::Auto,
    };
    assert!(matches!(
        KvIndexer::new(invalid, Some([7; 32])),
        Err(KvRuntimeConfigError::BindingSourceMissing { route_id, event_source_id })
            if route_id == "route" && event_source_id == "missing"
    ));
}

#[test]
fn auto_resolution_uses_owner_scope_and_readable_tier_capability() {
    let primary = source("http://unused".into());
    let mut independent_owner = primary.clone();
    independent_owner.event_source_id = "source-b".into();
    independent_owner.scope_id = "other-scope".into();
    let independent = KvIndexer::new(
        KvRuntimeConfig {
            event_sources: vec![primary.clone(), independent_owner],
            route_bindings: BTreeMap::new(),
            requested_implementation: KvLocalityIndexImplementation::Auto,
        },
        Some([7; 32]),
    )
    .expect("independent source owners are valid");
    assert_eq!(
        independent.status().auto_resolution_reason,
        Some(KvAutoResolutionReason::IndependentPrimarySources)
    );

    let mut same_scope_owner = primary.clone();
    same_scope_owner.event_source_id = "source-b".into();
    let shared_scope = KvIndexer::new(
        KvRuntimeConfig {
            event_sources: vec![primary.clone(), same_scope_owner],
            route_bindings: BTreeMap::new(),
            requested_implementation: KvLocalityIndexImplementation::Auto,
        },
        Some([7; 32]),
    )
    .expect("distinct source owners in one scope are valid");
    assert_eq!(
        shared_scope.status().auto_resolution_reason,
        Some(KvAutoResolutionReason::MultipleSourcesInScope)
    );

    let tier_capable = KvIndexer::new(
        KvRuntimeConfig {
            event_sources: vec![primary],
            route_bindings: BTreeMap::from([(
                "route-not-owner".into(),
                KvRouteBinding {
                    data_parallel_rank_event_source_ids: BTreeMap::from([(0, "source-a".into())]),
                    readable_placements: BTreeSet::from([foretoken_model_protocol::KvPlacement {
                        tier: foretoken_model_protocol::KvStorageTier::Disk,
                        locality: foretoken_model_protocol::KvCacheLocality::Remote,
                    }]),
                    can_restore_or_transfer: true,
                },
            )]),
            requested_implementation: KvLocalityIndexImplementation::Auto,
        },
        Some([7; 32]),
    )
    .expect("tier-capable owner binding is valid");
    assert_eq!(
        tier_capable.status().auto_resolution_reason,
        Some(KvAutoResolutionReason::ReadableTierContinuation)
    );
}

#[tokio::test]
async fn zero_based_cursor_rejects_duplicate_gap_reorder_and_invalid_epoch_reset() {
    let response = page(0, "one");
    let encoded = serde_json::to_value(&response).expect("serialize test response");
    serde_json::from_value::<KvDeltaResponse>(encoded).expect("deserialize test response");
    for (pages, reason) in [
        (
            vec![page(0, "one"), page(0, "one")],
            KvIndexDegradedReason::DeltaSequenceInvalid,
        ),
        (
            vec![page(0, "one"), page(2, "one")],
            KvIndexDegradedReason::DeltaSequenceGap,
        ),
        (
            vec![page(0, "one"), pages(&[1, 0], "one")],
            KvIndexDegradedReason::DeltaSequenceInvalid,
        ),
        (
            vec![page(0, "one"), page(0, "two"), page(1, "three")],
            KvIndexDegradedReason::DeltaCursorReset,
        ),
    ] {
        let (url, server) = endpoint(pages).await;
        let indexer = KvIndexer::new(runtime(url), Some([7; 32])).expect("valid runtime");
        indexer.refresh().await;
        let first_status = indexer.status();
        assert_eq!(
            first_status.state,
            KvIndexState::Healthy,
            "first page must be valid before checking {reason:?}: {first_status:?}"
        );
        indexer.refresh().await;
        if reason == KvIndexDegradedReason::DeltaCursorReset {
            assert_eq!(indexer.status().state, KvIndexState::Healthy);
            indexer.refresh().await;
        }
        let status = indexer.status();
        assert_eq!(status.state, KvIndexState::Degraded);
        assert_eq!(status.reason, Some(reason));
        assert_eq!(status.sources_healthy, 0);
        server.abort();
    }
}

#[test]
fn multi_rank_route_binding_requires_an_explicit_exact_rank() {
    let rank0 = source("http://unused".into());
    let mut rank1 = rank0.clone();
    rank1.event_source_id = "source-b".into();
    rank1.dp_rank = 1;
    let indexer = KvIndexer::new(
        KvRuntimeConfig {
            event_sources: vec![rank0, rank1],
            route_bindings: BTreeMap::from([(
                "route".into(),
                KvRouteBinding {
                    data_parallel_rank_event_source_ids: BTreeMap::from([
                        (0, "source-a".into()),
                        (1, "source-b".into()),
                    ]),
                    readable_placements: Default::default(),
                    can_restore_or_transfer: false,
                },
            )]),
            requested_implementation: KvLocalityIndexImplementation::Auto,
        },
        Some([7; 32]),
    )
    .expect("rank-specific sources are valid");
    let lookup = |data_parallel_rank| KvPrefixLookup {
        route_target_id: "route",
        data_parallel_rank,
        prompt_token_ids: &[],
    };
    assert_eq!(
        indexer.prefix_matches(lookup(2)),
        KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::RankMismatch)
    );
    assert_eq!(
        indexer.prefix_matches(lookup(1)),
        KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::SourceUnhealthy)
    );
}

#[tokio::test]
async fn hanging_source_is_timed_out_without_blocking_healthy_source_or_next_refresh() {
    async fn hang() -> axum::response::Response {
        std::future::pending().await
    }
    async fn healthy() -> Json<KvDeltaResponse> {
        Json(KvDeltaResponse {
            event_source_id: "healthy".into(),
            model_group_id: "owner".into(),
            epoch: "epoch".into(),
            dp_rank: 0,
            through: 0,
            current: 0,
            deltas: vec![],
        })
    }

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let address = listener.local_addr().unwrap();
    let server = tokio::spawn(async move {
        axum::serve(
            listener,
            Router::new()
                .route(&format!("/hang{KV_INDEX_DELTA_PATH}"), get(hang))
                .route(&format!("/healthy{KV_INDEX_DELTA_PATH}"), get(healthy)),
        )
        .await
        .unwrap()
    });
    let mut hanging = source(format!("http://{address}/hang"));
    hanging.event_source_id = "hanging".into();
    let mut healthy_source = source(format!("http://{address}/healthy"));
    healthy_source.event_source_id = "healthy".into();
    healthy_source.model_group_id = "owner".into();
    let indexer = KvIndexer::new(
        KvRuntimeConfig {
            event_sources: vec![hanging, healthy_source],
            route_bindings: BTreeMap::new(),
            requested_implementation: KvLocalityIndexImplementation::Auto,
        },
        Some([7; 32]),
    )
    .unwrap();

    tokio::time::timeout(std::time::Duration::from_secs(6), indexer.refresh())
        .await
        .expect("a hanging source is bounded");
    assert_eq!(indexer.status().sources_healthy, 1);
    tokio::time::timeout(std::time::Duration::from_secs(6), indexer.refresh())
        .await
        .expect("a prior timeout does not block the next refresh");
    assert_eq!(indexer.status().sources_healthy, 1);
    server.abort();
}
