// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Public KV scorer contract tests.

use foretoken_kv_indexer::{
    KvPrefixIndexer, KvPrefixLookup, KvPrefixMatch, KvPrefixMatches, KvPrefixQueryResult,
    KvPrefixUnavailableReason,
};
use foretoken_model_protocol::{KvCacheLocality, KvPlacement, KvStorageTier, ModelServerRole};
use foretoken_router::algorithm::LeastLoadedScorer;
use foretoken_router::{
    KvLeastLoadedScorer, NoopRouteTargetStatsReader, PipelineRouter, RouteCandidate, RouteScorer,
    RouteTargetId, RouteTargetLoad, Router,
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

fn candidate(id: &str, role: ModelServerRole, load: u64) -> RouteCandidate {
    let route = route(id, role);
    RouteCandidate {
        route_target_id: route.route_target_id,
        target: route.target,
        role,
        model: route.model,
        revision: route.revision,
        domain_id: route.domain_id,
        data_parallel_rank: 0,
        route_target_load: Some(RouteTargetLoad {
            running_requests: Some(load),
        }),
    }
}

#[test]
fn kv_scoring_is_prefix_tier_locality_load_and_keeps_unavailable_candidates() {
    let scored = KvLeastLoadedScorer.score(
        &request(),
        vec![
            candidate("remote", ModelServerRole::Aggregate, 1),
            candidate("local", ModelServerRole::Aggregate, 8),
            candidate("longer-disk", ModelServerRole::Aggregate, 99),
            candidate("unspecified", ModelServerRole::Aggregate, 0),
            candidate("unavailable", ModelServerRole::Aggregate, 0),
            candidate("decode", ModelServerRole::Decode, 0),
        ],
        &PrefixFacts,
        &NoopRouteTargetStatsReader,
        &mut (),
    );
    let score = |id: &str| {
        scored
            .iter()
            .find(|candidate| candidate.candidate.route_target_id == RouteTargetId::new(id))
            .expect("each scorer input remains available to the picker")
            .score
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
fn prefill_downstream_load_is_scoped_to_its_execution_domain() {
    let candidates = || {
        let mut prefill_a = candidate("prefill-a", ModelServerRole::Prefill, 0);
        let mut decode_a = candidate("decode-a", ModelServerRole::Decode, 100);
        let mut prefill_b = candidate("prefill-b", ModelServerRole::Prefill, 10);
        let mut decode_b = candidate("decode-b", ModelServerRole::Decode, 1);
        prefill_a.domain_id = Some("domain-a".into());
        decode_a.domain_id = Some("domain-a".into());
        prefill_b.domain_id = Some("domain-b".into());
        decode_b.domain_id = Some("domain-b".into());
        vec![prefill_a, decode_a, prefill_b, decode_b]
    };

    let request = request();
    let scored = [
        LeastLoadedScorer.score(
            &request,
            candidates(),
            &PrefixFacts,
            &NoopRouteTargetStatsReader,
            &mut (),
        ),
        KvLeastLoadedScorer.score(
            &request,
            candidates(),
            &PrefixFacts,
            &NoopRouteTargetStatsReader,
            &mut (),
        ),
    ];

    for round in scored {
        let score = |id: &str| {
            round
                .iter()
                .find(|candidate| candidate.candidate.route_target_id == RouteTargetId::new(id))
                .expect("each scorer input remains available")
                .score
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

#[test]
fn data_parallel_kv_rank_winner_is_selected_from_an_exact_rank_query() {
    let mut aggregate = route("dp-two", ModelServerRole::Aggregate);
    aggregate.data_parallel_size = 2;
    let (inventory, _) = inventory(vec![aggregate]);
    let router =
        PipelineRouter::new(inventory).with_kv_prefix_indexer(std::sync::Arc::new(RankFacts));

    let selected = router.start(request()).select_initial().unwrap();

    assert_eq!(selected.route_target_id, RouteTargetId::new("dp-two"));
    assert_eq!(selected.data_parallel_rank, 1);
}
