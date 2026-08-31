// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Public KV scorer behavior tests.

use std::sync::Arc;
use std::time::Duration;

use foretoken_kv_indexer::{
    KvPrefixIndexer, KvPrefixLookup, KvPrefixMatch, KvPrefixMatches, KvPrefixQueryResult,
    KvPrefixUnavailableReason,
};
use foretoken_model_protocol::{KvCacheLocality, KvPlacement, KvStorageTier, ModelServerRole};
use foretoken_router::algorithm::LeastLoadedScorer;
use foretoken_router::{
    KvLeastLoadedScorer, PipelineRouter, RouteCandidate, RouteScorer, RouteTargetId,
    RouteTargetStats, Router,
};

use super::support::{inventory, request, route};

struct PrefixFacts;

impl KvPrefixIndexer for PrefixFacts {
    fn prefix_matches(&self, lookup: KvPrefixLookup<'_>) -> KvPrefixQueryResult {
        match lookup.route_target_id {
            "remote" => KvPrefixQueryResult::Matches(KvPrefixMatches::new(vec![prefix(
                8,
                KvStorageTier::Device,
                KvCacheLocality::Remote,
            )])),
            "local" => KvPrefixQueryResult::Matches(KvPrefixMatches::new(vec![prefix(
                8,
                KvStorageTier::Device,
                KvCacheLocality::Local,
            )])),
            "longer-disk" => KvPrefixQueryResult::Matches(KvPrefixMatches::new(vec![prefix(
                9,
                KvStorageTier::Disk,
                KvCacheLocality::Remote,
            )])),
            "unspecified" => KvPrefixQueryResult::Matches(KvPrefixMatches::new(vec![prefix(
                8,
                KvStorageTier::Device,
                KvCacheLocality::Unspecified,
            )])),
            "decode" => KvPrefixQueryResult::Matches(KvPrefixMatches::new(vec![prefix(
                99,
                KvStorageTier::Device,
                KvCacheLocality::Local,
            )])),
            _ => KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::SourceUnhealthy),
        }
    }
}

fn prefix(tokens: usize, tier: KvStorageTier, locality: KvCacheLocality) -> KvPrefixMatch {
    KvPrefixMatch {
        event_source_id: "source".into(),
        model_group_id: "owner".into(),
        epoch: "epoch".into(),
        dp_rank: 0,
        placement: KvPlacement { tier, locality },
        matched_complete_blocks: 1,
        matched_tokens: tokens,
        last_matched_hash: None,
    }
}

// Protects cache locality from requests whose cache identity cannot be shared safely.
#[test]
fn kv_lookup_rejects_requests_with_separate_cache_semantics() {
    let mut salted = request();
    Arc::get_mut(&mut salted.generate_request)
        .expect("test request has one owner")
        .cache_salt = Some("tenant-a".into());
    assert!(matches!(
        salted.kv_prefix_lookup("target", 0),
        Err(KvPrefixUnavailableReason::UnsupportedRequest)
    ));

    let mut disabled = request();
    Arc::get_mut(&mut disabled.generate_request)
        .expect("test request has one owner")
        .sampling_params
        .skip_reading_prefix_cache = Some(true);
    assert!(matches!(
        disabled.kv_prefix_lookup("target", 0),
        Err(KvPrefixUnavailableReason::UnsupportedRequest)
    ));
}

fn target_stats(running_requests: u64) -> Arc<RouteTargetStats> {
    Arc::new(RouteTargetStats {
        collected_at_unix_ms: 1,
        observed_window: Duration::from_secs(60),
        running_requests,
        max_concurrent_requests: 8,
        scheduler_running_requests: None,
        scheduler_waiting_requests: None,
        kv_cache_usage: None,
        prompt_tokens_per_second: None,
        generation_tokens_per_second: None,
        ttft: None,
        tpot: None,
        e2e_latency: None,
    })
}

fn candidate(id: &str, role: ModelServerRole, load: u64) -> RouteCandidate {
    let route = route(id, role);
    RouteCandidate {
        route_target_id: route.route_target_id,
        target: route.target,
        admission_targets: route.admission_targets,
        role,
        model: route.model,
        revision: route.revision,
        pipeline_scope_id: route.pipeline_scope_id,
        data_parallel_rank: 0,
        route_target_stats: Some(target_stats(load)),
    }
}

// Protects KV scoring order and keeps unavailable locality distinct from a confirmed miss.
#[test]
fn kv_scoring_is_prefix_tier_locality_load_and_keeps_unavailable_candidates() {
    let candidates = vec![
        candidate("remote", ModelServerRole::Aggregate, 1),
        candidate("local", ModelServerRole::Aggregate, 8),
        candidate("longer-disk", ModelServerRole::Aggregate, 99),
        candidate("unspecified", ModelServerRole::Aggregate, 0),
        candidate("unavailable", ModelServerRole::Aggregate, 0),
        candidate("decode", ModelServerRole::Decode, 0),
    ];
    let scored = KvLeastLoadedScorer.score(&request(), &candidates, &PrefixFacts, &mut ());
    let score = |id: &str| {
        let index = [
            "remote",
            "local",
            "longer-disk",
            "unspecified",
            "unavailable",
            "decode",
        ]
        .iter()
        .position(|candidate_id| *candidate_id == id)
        .expect("known scorer input");
        scored[index]
    };

    assert!(score("longer-disk") > score("local"));
    assert!(score("local") > score("remote"));
    assert!(score("remote") > score("unavailable"));
    assert_eq!(score("unspecified"), score("unavailable"));
    assert_eq!(score("unspecified").matched_tokens, 0);
    assert_eq!(score("unavailable").matched_tokens, 0);
    assert_eq!(score("decode").matched_tokens, 0);
}

// Protects prefill scoring from using Decode load in another pipeline scope.
#[test]
fn prefill_downstream_load_is_scoped_to_its_pipeline_scope() {
    let candidates = || {
        let mut prefill_a = candidate("prefill-a", ModelServerRole::Prefill, 0);
        let mut decode_a = candidate("decode-a", ModelServerRole::Decode, 100);
        let mut prefill_b = candidate("prefill-b", ModelServerRole::Prefill, 10);
        let mut decode_b = candidate("decode-b", ModelServerRole::Decode, 1);
        prefill_a.pipeline_scope_id = Some("pipeline-scope-a".into());
        decode_a.pipeline_scope_id = Some("pipeline-scope-a".into());
        prefill_b.pipeline_scope_id = Some("pipeline-scope-b".into());
        decode_b.pipeline_scope_id = Some("pipeline-scope-b".into());
        vec![prefill_a, decode_a, prefill_b, decode_b]
    };

    let request = request();
    let candidates = candidates();
    let scored = [
        LeastLoadedScorer.score(&request, &candidates, &PrefixFacts, &mut ()),
        KvLeastLoadedScorer.score(&request, &candidates, &PrefixFacts, &mut ()),
    ];

    for round in scored {
        let score = |id: &str| {
            let index = match id {
                "prefill-a" => 0,
                "decode-a" => 1,
                "prefill-b" => 2,
                "decode-b" => 3,
                _ => panic!("unknown scorer input"),
            };
            round[index]
        };
        assert!(score("prefill-b") > score("prefill-a"));
    }
}

// Protects load-aware routing from ignoring requests queued inside the vLLM scheduler.
#[test]
fn load_scoring_uses_scheduler_backlog_without_double_counting_admission() {
    let mut idle = candidate("idle", ModelServerRole::Aggregate, 1);
    let mut queued = candidate("queued", ModelServerRole::Aggregate, 2);
    Arc::get_mut(idle.route_target_stats.as_mut().unwrap())
        .unwrap()
        .scheduler_running_requests = Some(1);
    let queued_stats = Arc::get_mut(queued.route_target_stats.as_mut().unwrap()).unwrap();
    queued_stats.scheduler_running_requests = Some(2);
    queued_stats.scheduler_waiting_requests = Some(5);

    let candidates = vec![idle, queued];
    let scores = LeastLoadedScorer.score(&request(), &candidates, &PrefixFacts, &mut ());

    assert_eq!(scores[0].load, -1);
    assert_eq!(scores[1].load, -7);
    assert!(scores[0] > scores[1]);
}

struct RankFacts;

impl KvPrefixIndexer for RankFacts {
    fn prefix_matches(&self, lookup: KvPrefixLookup<'_>) -> KvPrefixQueryResult {
        let matched_tokens = if lookup.data_parallel_rank == 1 {
            16
        } else {
            1
        };
        KvPrefixQueryResult::Matches(KvPrefixMatches::new(vec![prefix(
            matched_tokens,
            KvStorageTier::Device,
            KvCacheLocality::Local,
        )]))
    }
}

// Protects data-parallel routing from collapsing rank-specific KV locality.
#[test]
fn data_parallel_kv_rank_winner_is_selected_from_an_exact_rank_query() {
    let mut aggregate = route("dp-two", ModelServerRole::Aggregate);
    aggregate.data_parallel_size = 2;
    let router = PipelineRouter::new(inventory(vec![aggregate]))
        .with_kv_prefix_indexer(std::sync::Arc::new(RankFacts));

    let selected = router.start(request()).select_initial().unwrap();

    assert_eq!(selected.route_target_id, RouteTargetId::new("dp-two"));
    assert_eq!(selected.data_parallel_rank, 1);
}
