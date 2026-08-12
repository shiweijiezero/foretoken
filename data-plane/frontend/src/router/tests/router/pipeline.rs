// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Pipeline visibility and customized-context tests.

use std::sync::{Arc, Mutex};

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use super::support::{inventory, request, route};
use foretoken_router::{
    PipelineRouter, RouteCandidate, RouteFilter, RoutePicker, RouteScore, RouteScorer,
    RouteTargetStatsReader, Router, RouterPipeline, RouterRequest, ScoredCandidate,
};

#[derive(Default)]
struct RecordingContext {
    rounds: usize,
    picker_rounds: Arc<Mutex<Vec<usize>>>,
    scorer_roles: Arc<Mutex<Vec<Vec<ModelServerRole>>>>,
    picker_roles: Arc<Mutex<Vec<Vec<ModelServerRole>>>>,
}

struct RecordingFilter;

impl RouteFilter<RecordingContext> for RecordingFilter {
    #[allow(unused_variables)]
    fn filter(
        &self,
        request: &RouterRequest,
        candidates: Vec<RouteCandidate>,
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        route_target_stats_reader: &dyn RouteTargetStatsReader,
        customized_context: &mut RecordingContext,
    ) -> Vec<RouteCandidate> {
        customized_context.rounds += 1;
        candidates
    }
}

struct RecordingScorer;

impl RouteScorer<RecordingContext> for RecordingScorer {
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: Vec<RouteCandidate>,
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        route_target_stats_reader: &dyn RouteTargetStatsReader,
        customized_context: &mut RecordingContext,
    ) -> Vec<ScoredCandidate> {
        customized_context
            .scorer_roles
            .lock()
            .unwrap()
            .push(candidates.iter().map(|candidate| candidate.role).collect());
        candidates
            .into_iter()
            .map(|candidate| ScoredCandidate {
                candidate,
                score: RouteScore::default(),
            })
            .collect()
    }
}

struct RecordingPicker;

impl RoutePicker<RecordingContext> for RecordingPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        customized_context: &mut RecordingContext,
    ) -> Option<RouteCandidate> {
        customized_context
            .picker_rounds
            .lock()
            .unwrap()
            .push(customized_context.rounds);
        customized_context.picker_roles.lock().unwrap().push(
            scored_candidates
                .iter()
                .map(|candidate| candidate.candidate.role)
                .collect(),
        );
        scored_candidates
            .first()
            .map(|candidate| candidate.candidate.clone())
    }
}

#[test]
fn list_algorithms_see_all_nodes_across_explicit_encoder_prefill_fresh_decode() {
    let (inventory, _) = inventory(vec![
        route("e", ModelServerRole::Encoder),
        route("p", ModelServerRole::Prefill),
        route("d", ModelServerRole::Decode),
    ]);
    let picker_rounds = Arc::new(Mutex::new(Vec::new()));
    let scorer_roles = Arc::new(Mutex::new(Vec::new()));
    let picker_roles = Arc::new(Mutex::new(Vec::new()));
    let pipeline = RouterPipeline::with_customized_context(
        Arc::new(RecordingFilter),
        Arc::new(RecordingScorer),
        Arc::new(RecordingPicker),
        {
            let picker_rounds = picker_rounds.clone();
            let scorer_roles = scorer_roles.clone();
            let picker_roles = picker_roles.clone();
            move |_| RecordingContext {
                picker_rounds: picker_rounds.clone(),
                scorer_roles: scorer_roles.clone(),
                picker_roles: picker_roles.clone(),
                ..Default::default()
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
    assert_eq!(*picker_rounds.lock().unwrap(), vec![1, 2, 3]);
    assert_eq!(
        *scorer_roles.lock().unwrap(),
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
        *picker_roles.lock().unwrap(),
        vec![
            vec![ModelServerRole::Encoder],
            vec![ModelServerRole::Prefill],
            vec![ModelServerRole::Decode],
        ]
    );
}
