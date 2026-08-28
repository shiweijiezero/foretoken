// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Malformed routing algorithm output tests.

use std::sync::Arc;

use foretoken_kv_indexer::KvPrefixIndexer;
use foretoken_model_protocol::ModelServerRole;

use super::support::{inventory, request, route};
use foretoken_router::algorithm::{AllowAllFilter, UniformScorer};
use foretoken_router::{
    CandidateIndex, PipelineRouter, RouteCandidate, RouteError, RouteFilter, RoutePicker,
    RouteScore, RouteScorer, Router, RouterPipeline, RouterRequest, ScoredCandidate,
};

struct InvalidPicker;

impl RoutePicker for InvalidPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        customized_context: &mut (),
    ) -> Option<CandidateIndex> {
        Some(CandidateIndex(scored_candidates.len()))
    }
}

struct EmptyPicker;

impl RoutePicker for EmptyPicker {
    #[allow(unused_variables)]
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        customized_context: &mut (),
    ) -> Option<CandidateIndex> {
        None
    }
}

struct InvalidFilter;

impl RouteFilter for InvalidFilter {
    #[allow(unused_variables)]
    fn filter(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        customized_context: &mut (),
    ) -> Vec<CandidateIndex> {
        vec![CandidateIndex(candidates.len())]
    }
}

struct DuplicateFilter;

impl RouteFilter for DuplicateFilter {
    #[allow(unused_variables)]
    fn filter(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        customized_context: &mut (),
    ) -> Vec<CandidateIndex> {
        vec![CandidateIndex(0), CandidateIndex(0)]
    }
}

struct InvalidScorer;

impl RouteScorer for InvalidScorer {
    #[allow(unused_variables)]
    fn score(
        &self,
        request: &RouterRequest,
        candidates: &[RouteCandidate],
        kv_prefix_indexer: &dyn KvPrefixIndexer,
        customized_context: &mut (),
    ) -> Vec<RouteScore> {
        vec![]
    }
}

// Protects extension algorithms from corrupting routing with invalid indexes or score counts.
#[test]
fn malformed_algorithm_outputs_are_explicit_errors() {
    let make_router = |filter: Arc<dyn RouteFilter>, scorer: Arc<dyn RouteScorer>, picker| {
        let inventory = inventory(vec![route("a", ModelServerRole::Aggregate)]);
        PipelineRouter::with_pipeline(inventory, RouterPipeline::new(filter, scorer, picker))
    };

    assert_eq!(
        make_router(
            Arc::new(InvalidFilter),
            Arc::new(UniformScorer),
            Arc::new(InvalidPicker),
        )
        .start(request())
        .select_initial(),
        Err(RouteError::InvalidFilterIndex { index: 1 })
    );
    assert_eq!(
        make_router(
            Arc::new(DuplicateFilter),
            Arc::new(UniformScorer),
            Arc::new(InvalidPicker),
        )
        .start(request())
        .select_initial(),
        Err(RouteError::DuplicateFilterIndex { index: 0 })
    );
    assert_eq!(
        make_router(
            Arc::new(AllowAllFilter),
            Arc::new(InvalidScorer),
            Arc::new(InvalidPicker),
        )
        .start(request())
        .select_initial(),
        Err(RouteError::InvalidScorerResult {
            expected: 1,
            actual: 0,
        })
    );
    assert_eq!(
        make_router(
            Arc::new(AllowAllFilter),
            Arc::new(UniformScorer),
            Arc::new(EmptyPicker),
        )
        .start(request())
        .select_initial(),
        Err(RouteError::EmptyPickerResult)
    );
    assert_eq!(
        make_router(
            Arc::new(AllowAllFilter),
            Arc::new(UniformScorer),
            Arc::new(InvalidPicker),
        )
        .start(request())
        .select_initial(),
        Err(RouteError::InvalidPickerIndex { index: 1 })
    );
}
