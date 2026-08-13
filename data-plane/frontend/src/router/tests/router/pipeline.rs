// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Customized-context contract for a complete E/P/D routing session.

use std::sync::{Arc, Mutex};

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use std::time::Duration;

use super::support::{TestStatsReader, inventory, request, route, stats};
use foretoken_router::{
    CandidateIndex, PipelineRouter, RouteCandidate, RouteFilter, RoutePicker, RouteScore,
    RouteScorer, RouteTargetId, RouteTargetStats, Router, RouterPipeline, RouterRequest,
    ScoredCandidate,
};

type CandidateObservations = Vec<Vec<(Option<u64>, Option<usize>)>>;

#[derive(Default)]
struct ContextTrace {
    events: Mutex<Vec<String>>,
    scorer_roles: Mutex<Vec<Vec<ModelServerRole>>>,
    filter_observations: Mutex<CandidateObservations>,
    scorer_observations: Mutex<CandidateObservations>,
    picker_roles: Mutex<Vec<Vec<ModelServerRole>>>,
}

struct RoutingContext {
    request_id: String,
    rounds: usize,
    scorer_round: usize,
    trace: Arc<ContextTrace>,
}

struct ContextFilter;

impl RouteFilter<RoutingContext> for ContextFilter {
    fn filter(
        &self,
        _: &RouterRequest,
        candidates: &[RouteCandidate],
        _: &dyn KvPrefixIndexer,
        context: &mut RoutingContext,
    ) -> Vec<CandidateIndex> {
        context.rounds += 1;
        context.trace.filter_observations.lock().unwrap().push(
            candidates
                .iter()
                .map(|candidate| {
                    (
                        candidate
                            .route_target_stats
                            .as_ref()
                            .map(|stats| stats.running_requests),
                        candidate
                            .route_target_stats
                            .as_ref()
                            .map(|stats| Arc::as_ptr(stats) as usize),
                    )
                })
                .collect(),
        );
        context
            .trace
            .events
            .lock()
            .unwrap()
            .push(format!("{}:filter:{}", context.request_id, context.rounds));
        (0..candidates.len()).map(CandidateIndex).collect()
    }
}

struct ContextScorer;

impl RouteScorer<RoutingContext> for ContextScorer {
    fn score(
        &self,
        _: &RouterRequest,
        candidates: &[RouteCandidate],
        _: &dyn KvPrefixIndexer,
        context: &mut RoutingContext,
    ) -> Vec<RouteScore> {
        // Consume the round established by Filter. Picker consumes this value below.
        context.scorer_round = context.rounds;
        context.trace.events.lock().unwrap().push(format!(
            "{}:scorer:{}",
            context.request_id, context.scorer_round
        ));
        context
            .trace
            .scorer_roles
            .lock()
            .unwrap()
            .push(candidates.iter().map(|candidate| candidate.role).collect());
        context.trace.scorer_observations.lock().unwrap().push(
            candidates
                .iter()
                .map(|candidate| {
                    (
                        candidate
                            .route_target_stats
                            .as_ref()
                            .map(|stats| stats.running_requests),
                        candidate
                            .route_target_stats
                            .as_ref()
                            .map(|stats| Arc::as_ptr(stats) as usize),
                    )
                })
                .collect(),
        );
        vec![RouteScore::default(); candidates.len()]
    }
}

struct ContextPicker;

impl RoutePicker<RoutingContext> for ContextPicker {
    fn pick(
        &self,
        _: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        context: &mut RoutingContext,
    ) -> Option<CandidateIndex> {
        assert_eq!(context.scorer_round, context.rounds);
        context.trace.events.lock().unwrap().push(format!(
            "{}:picker:{}",
            context.request_id, context.scorer_round
        ));
        context.trace.picker_roles.lock().unwrap().push(
            scored_candidates
                .iter()
                .map(|candidate| candidate.candidate.role)
                .collect(),
        );
        (!scored_candidates.is_empty()).then_some(CandidateIndex(0))
    }
}

#[test]
fn customized_context_is_request_owned_and_shared_by_filter_scorer_picker_across_epd() {
    let inventory = inventory(vec![
        route("e", ModelServerRole::Encoder),
        route("p", ModelServerRole::Prefill),
        route("d", ModelServerRole::Decode),
    ]);
    let trace = Arc::new(ContextTrace::default());
    let stat_values = stats();
    stat_values.lock().unwrap().extend([
        (
            RouteTargetId::new("e"),
            RouteTargetStats {
                collected_at_unix_ms: 1,
                observed_window: Duration::from_secs(60),
                running_requests: 3,
                max_concurrent_requests: 8,
                scheduler_running_requests: Some(2),
                scheduler_waiting_requests: Some(1),
                kv_cache_usage: Some(0.5),
                prompt_tokens_per_second: Some(100.0),
                generation_tokens_per_second: Some(50.0),
                ttft: None,
                tpot: None,
                e2e_latency: None,
            },
        ),
        (
            RouteTargetId::new("p"),
            RouteTargetStats {
                collected_at_unix_ms: 1,
                observed_window: Duration::from_secs(60),
                running_requests: 3,
                max_concurrent_requests: 8,
                scheduler_running_requests: Some(2),
                scheduler_waiting_requests: Some(1),
                kv_cache_usage: Some(0.5),
                prompt_tokens_per_second: Some(100.0),
                generation_tokens_per_second: Some(50.0),
                ttft: None,
                tpot: None,
                e2e_latency: None,
            },
        ),
        (
            RouteTargetId::new("d"),
            RouteTargetStats {
                collected_at_unix_ms: 1,
                observed_window: Duration::from_secs(60),
                running_requests: 3,
                max_concurrent_requests: 8,
                scheduler_running_requests: Some(2),
                scheduler_waiting_requests: Some(1),
                kv_cache_usage: Some(0.5),
                prompt_tokens_per_second: Some(100.0),
                generation_tokens_per_second: Some(50.0),
                ttft: None,
                tpot: None,
                e2e_latency: None,
            },
        ),
    ]);
    let pipeline = RouterPipeline::with_customized_context(
        Arc::new(ContextFilter),
        Arc::new(ContextScorer),
        Arc::new(ContextPicker),
        {
            let trace = trace.clone();
            move |request| RoutingContext {
                request_id: request.generate_request.request_id.clone(),
                rounds: 0,
                scorer_round: 0,
                trace: trace.clone(),
            }
        },
    );
    let router = PipelineRouter::with_pipeline(inventory, pipeline)
        .with_route_target_stats_reader(Arc::new(TestStatsReader::new(stat_values)));
    let mut session = router.start(request());

    assert_eq!(
        session.select_initial().unwrap().role,
        ModelServerRole::Encoder
    );
    assert_eq!(
        session.select_prefill().unwrap().role,
        ModelServerRole::Prefill
    );
    assert_eq!(
        session.select_decode().unwrap().role,
        ModelServerRole::Decode
    );
    assert_eq!(
        *trace.events.lock().unwrap(),
        vec![
            "request:filter:1",
            "request:scorer:1",
            "request:picker:1",
            "request:filter:2",
            "request:scorer:2",
            "request:picker:2",
            "request:filter:3",
            "request:scorer:3",
            "request:picker:3",
        ]
    );
    assert_eq!(
        *trace.scorer_roles.lock().unwrap(),
        vec![
            vec![
                ModelServerRole::Decode,
                ModelServerRole::Encoder,
                ModelServerRole::Prefill
            ],
            vec![
                ModelServerRole::Decode,
                ModelServerRole::Encoder,
                ModelServerRole::Prefill
            ],
            vec![
                ModelServerRole::Decode,
                ModelServerRole::Encoder,
                ModelServerRole::Prefill
            ],
        ]
    );
    assert_eq!(
        *trace.filter_observations.lock().unwrap(),
        *trace.scorer_observations.lock().unwrap()
    );
    assert!(
        trace
            .scorer_observations
            .lock()
            .unwrap()
            .iter()
            .flatten()
            .all(|(running_requests, _)| *running_requests == Some(3))
    );
    assert_eq!(
        *trace.picker_roles.lock().unwrap(),
        vec![
            vec![ModelServerRole::Encoder],
            vec![ModelServerRole::Prefill],
            vec![ModelServerRole::Decode],
        ]
    );
}
