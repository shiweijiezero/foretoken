// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Customized-context contract for a complete E/P/D routing session.

use std::sync::{Arc, Mutex};

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use super::support::{inventory, request, route};
use foretoken_router::{
    CandidateIndex, PipelineRouter, RouteCandidate, RouteFilter, RoutePicker, RouteScore,
    RouteScorer, Router, RouterPipeline, RouterRequest, RoutingProgress, RoutingStage,
    ScoredCandidate,
};

#[derive(Debug, PartialEq, Eq)]
struct ScorerRound {
    stage: RoutingStage,
    completed_stages: Vec<ModelServerRole>,
    pipeline_scope_id: Option<String>,
    candidates: Vec<(ModelServerRole, Vec<ModelServerRole>)>,
}

#[derive(Default)]
struct ContextTrace {
    events: Mutex<Vec<String>>,
    scorer_rounds: Mutex<Vec<ScorerRound>>,
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
        _: &RoutingProgress<'_>,
        context: &mut RoutingContext,
    ) -> Vec<CandidateIndex> {
        context.rounds += 1;
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
        routing_progress: &RoutingProgress<'_>,
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
            .scorer_rounds
            .lock()
            .unwrap()
            .push(ScorerRound {
                stage: routing_progress.current_stage,
                completed_stages: routing_progress.completed_stages.to_vec(),
                pipeline_scope_id: routing_progress.pipeline_scope_id.map(str::to_owned),
                candidates: candidates
                    .iter()
                    .map(|candidate| (candidate.role, candidate.future_stages().to_vec()))
                    .collect(),
            });
        vec![RouteScore::default(); candidates.len()]
    }
}

struct ContextPicker;

impl RoutePicker<RoutingContext> for ContextPicker {
    fn pick(
        &self,
        _: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        _: &RoutingProgress<'_>,
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

// Protects request-local algorithm state and explicit stage context across E/P/D routing.
#[test]
fn algorithms_share_request_state_and_observe_each_epd_selection_stage() {
    let inventory = inventory(vec![
        route("e", ModelServerRole::Encoder),
        route("p", ModelServerRole::Prefill),
        route("d", ModelServerRole::Decode),
    ]);
    let trace = Arc::new(ContextTrace::default());
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
    let router = PipelineRouter::with_pipeline(inventory, pipeline);
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
    let candidates = || {
        vec![
            (ModelServerRole::Decode, vec![]),
            (
                ModelServerRole::Encoder,
                vec![ModelServerRole::Prefill, ModelServerRole::Decode],
            ),
            (ModelServerRole::Prefill, vec![ModelServerRole::Decode]),
        ]
    };
    assert_eq!(
        *trace.scorer_rounds.lock().unwrap(),
        vec![
            ScorerRound {
                stage: RoutingStage::Initial,
                completed_stages: vec![],
                pipeline_scope_id: None,
                candidates: candidates(),
            },
            ScorerRound {
                stage: RoutingStage::Prefill,
                completed_stages: vec![ModelServerRole::Encoder],
                pipeline_scope_id: Some("pipeline-scope-a".into()),
                candidates: candidates(),
            },
            ScorerRound {
                stage: RoutingStage::Decode,
                completed_stages: vec![ModelServerRole::Encoder, ModelServerRole::Prefill],
                pipeline_scope_id: Some("pipeline-scope-a".into()),
                candidates: candidates(),
            },
        ]
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
