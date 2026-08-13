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
    CandidateIndex, KvLeastLoadedScorer, PipelineRouter, RouteCandidate, RouteFilter, RoutePicker,
    RouteScore, RouteScorer, RouteTargetId, RouteTargetStats, Router, RouterPipeline,
    RouterRequest, ScoredCandidate,
};

use super::support::{TestStatsReader, inventory, request, route, stats};

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
        role,
        model: route.model,
        revision: route.revision,
        pipeline_scope_id: route.pipeline_scope_id,
        data_parallel_rank: 0,
        route_target_stats: Some(target_stats(load)),
    }
}

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

struct SnapshotFilter;

impl RouteFilter for SnapshotFilter {
    fn filter(
        &self,
        _: &RouterRequest,
        candidates: &[RouteCandidate],
        _: &dyn KvPrefixIndexer,
        _: &mut (),
    ) -> Vec<CandidateIndex> {
        assert_eq!(candidates.len(), 2);
        assert_eq!(
            candidates[0]
                .route_target_stats
                .as_ref()
                .map(|stats| stats.running_requests),
            Some(7)
        );
        assert!(Arc::ptr_eq(
            candidates[0].route_target_stats.as_ref().unwrap(),
            candidates[1].route_target_stats.as_ref().unwrap(),
        ));
        vec![CandidateIndex(0), CandidateIndex(1)]
    }
}

struct SnapshotScorer;

impl RouteScorer for SnapshotScorer {
    fn score(
        &self,
        _: &RouterRequest,
        candidates: &[RouteCandidate],
        _: &dyn KvPrefixIndexer,
        _: &mut (),
    ) -> Vec<RouteScore> {
        assert_eq!(candidates.len(), 2);
        assert_eq!(
            candidates[0]
                .route_target_stats
                .as_ref()
                .map(|stats| stats.running_requests),
            Some(7)
        );
        assert!(Arc::ptr_eq(
            candidates[0].route_target_stats.as_ref().unwrap(),
            candidates[1].route_target_stats.as_ref().unwrap(),
        ));
        vec![RouteScore::default(); candidates.len()]
    }
}

struct FirstPicker;

impl RoutePicker for FirstPicker {
    fn pick(&self, _: &RouterRequest, _: &[ScoredCandidate], _: &mut ()) -> Option<CandidateIndex> {
        Some(CandidateIndex(0))
    }
}

#[test]
fn data_parallel_candidates_share_one_target_observation_and_preserve_rank() {
    let mut aggregate = route("dp-two", ModelServerRole::Aggregate);
    aggregate.data_parallel_size = 2;
    let stat_values = stats();
    stat_values
        .lock()
        .unwrap()
        .insert(RouteTargetId::new("dp-two"), (*target_stats(7)).clone());
    let router = PipelineRouter::with_pipeline(
        inventory(vec![aggregate]),
        RouterPipeline::new(
            Arc::new(SnapshotFilter),
            Arc::new(SnapshotScorer),
            Arc::new(FirstPicker),
        ),
    )
    .with_route_target_stats_reader(Arc::new(TestStatsReader::new(stat_values)));

    let selected = router.start(request()).select_initial().unwrap();

    assert_eq!(selected.route_target_id, RouteTargetId::new("dp-two"));
    assert_eq!(selected.data_parallel_rank, 0);
}

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
