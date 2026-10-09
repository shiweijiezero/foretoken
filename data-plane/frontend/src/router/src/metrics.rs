// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Process-local routing observations, exported through the Frontend OpenMetrics endpoint.

use std::collections::{HashMap, HashSet};
use std::fmt;
use std::hash::Hash;
use std::sync::{LazyLock, Mutex};
use std::time::Duration;

use foretoken_model_protocol::ModelServerRole;
use prometheus_client::encoding::EncodeLabelSet;
use prometheus_client::encoding::text::encode;
use prometheus_client::metrics::counter::Counter;
use prometheus_client::metrics::family::{Family, MetricConstructor};
use prometheus_client::metrics::histogram::{Histogram, exponential_buckets};
use prometheus_client::registry::Registry;

use crate::{RouteCandidate, RouteError, RouteInventory, RoutingStage};

const ROUTING_STAGES: [RoutingStage; 3] = [
    RoutingStage::Initial,
    RoutingStage::Prefill,
    RoutingStage::Decode,
];
const OUTCOMES: [&str; 7] = [
    "selected",
    "no_matching_target",
    "no_matching_decode",
    "invalid_filter",
    "invalid_scorer",
    "invalid_picker",
    "invalid_sequence",
];
const CANDIDATE_STAGES: [&str; 3] = ["available", "filtered", "selectable"];

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct StageLabels {
    model_name: String,
    round: &'static str,
    stage: &'static str,
    algorithm: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct OutcomeLabels {
    model_name: String,
    round: &'static str,
    outcome: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct CandidateLabels {
    model_name: String,
    round: &'static str,
    stage: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct TargetLabels {
    model_name: String,
    model_role: &'static str,
    route_target_id: String,
    data_parallel_rank: u32,
}

#[derive(Default)]
struct LabelReferences {
    stages: HashMap<StageLabels, usize>,
    outcomes: HashMap<OutcomeLabels, usize>,
    candidates: HashMap<CandidateLabels, usize>,
    targets: HashMap<TargetLabels, usize>,
}

pub(crate) struct RouterMetrics {
    registry: Registry,
    stages: Family<StageLabels, Histogram, fn() -> Histogram>,
    selections: Family<OutcomeLabels, Counter>,
    duration: Family<OutcomeLabels, Histogram, fn() -> Histogram>,
    candidates: Family<CandidateLabels, Histogram, fn() -> Histogram>,
    target_selections: Family<TargetLabels, Counter>,
    references: Mutex<LabelReferences>,
}

fn latency_histogram() -> Histogram {
    Histogram::new(exponential_buckets(0.00001, 4.0, 10))
}

fn candidate_histogram() -> Histogram {
    Histogram::new(exponential_buckets(1.0, 2.0, 12))
}

impl RouterMetrics {
    // The registry and its handles share one process lifetime. A generation-scoped registration
    // keeps snapshot-derived model and target labels bounded while overlapping generations share
    // the same underlying metric handles.
    fn new() -> Self {
        let mut registry = Registry::default();
        let stages = Family::new_with_constructor(latency_histogram as fn() -> Histogram);
        let selections = Family::default();
        let duration = Family::new_with_constructor(latency_histogram as fn() -> Histogram);
        let candidates = Family::new_with_constructor(candidate_histogram as fn() -> Histogram);
        let target_selections = Family::default();
        registry.register(
            "foretoken_router_stage_duration_seconds",
            "Filter, scorer, and picker execution time",
            stages.clone(),
        );
        registry.register(
            "foretoken_router_selections",
            "Completed routing rounds by outcome",
            selections.clone(),
        );
        registry.register(
            "foretoken_router_selection_duration_seconds",
            "Complete routing round time including candidate discovery",
            duration.clone(),
        );
        registry.register(
            "foretoken_router_candidates",
            "Candidate count before filtering, after filtering, and before picking",
            candidates.clone(),
        );
        registry.register(
            "foretoken_router_target_selections",
            "Successful routing selections by model, role, route target, and data-parallel rank",
            target_selections.clone(),
        );
        Self {
            registry,
            stages,
            selections,
            duration,
            candidates,
            target_selections,
            references: Mutex::new(LabelReferences::default()),
        }
    }
}

/// Retains the fixed metric label space derived from one immutable routing generation.
pub(crate) struct RouterMetricsScope {
    stage_labels: Vec<StageLabels>,
    outcome_labels: Vec<OutcomeLabels>,
    candidate_labels: Vec<CandidateLabels>,
    target_labels: Vec<TargetLabels>,
}

impl RouterMetricsScope {
    /// Registers bounded model and target labels before the Router begins serving requests.
    pub(crate) fn new(inventory: &dyn RouteInventory, algorithms: [&'static str; 3]) -> Self {
        let routes = inventory.model_routes().routes();
        let models = routes
            .iter()
            .map(|route| route.model.clone())
            .collect::<HashSet<_>>();
        let mut stage_labels = HashSet::new();
        let mut outcome_labels = HashSet::new();
        let mut candidate_labels = HashSet::new();
        for model_name in models {
            for round in ROUTING_STAGES {
                for (stage, algorithm) in ["filter", "scorer", "picker"].into_iter().zip(algorithms)
                {
                    stage_labels.insert(StageLabels {
                        model_name: model_name.clone(),
                        round: round_name(round),
                        stage,
                        algorithm,
                    });
                }
                for outcome in OUTCOMES {
                    outcome_labels.insert(OutcomeLabels {
                        model_name: model_name.clone(),
                        round: round_name(round),
                        outcome,
                    });
                }
                for stage in CANDIDATE_STAGES {
                    candidate_labels.insert(CandidateLabels {
                        model_name: model_name.clone(),
                        round: round_name(round),
                        stage,
                    });
                }
            }
        }
        let target_labels = routes
            .iter()
            .flat_map(|route| {
                (0..route.data_parallel_size).map(|data_parallel_rank| TargetLabels {
                    model_name: route.model.clone(),
                    model_role: role_name(route.role),
                    route_target_id: route.route_target_id.as_str().to_owned(),
                    data_parallel_rank,
                })
            })
            .collect::<HashSet<_>>();

        let scope = Self {
            stage_labels: stage_labels.into_iter().collect(),
            outcome_labels: outcome_labels.into_iter().collect(),
            candidate_labels: candidate_labels.into_iter().collect(),
            target_labels: target_labels.into_iter().collect(),
        };
        let mut references = METRICS
            .references
            .lock()
            .expect("router metric references lock poisoned");
        retain_labels(&METRICS.stages, &mut references.stages, &scope.stage_labels);
        retain_labels(
            &METRICS.selections,
            &mut references.outcomes,
            &scope.outcome_labels,
        );
        for labels in &scope.outcome_labels {
            drop(METRICS.duration.get_or_create(labels));
        }
        retain_labels(
            &METRICS.candidates,
            &mut references.candidates,
            &scope.candidate_labels,
        );
        retain_labels(
            &METRICS.target_selections,
            &mut references.targets,
            &scope.target_labels,
        );
        drop(references);
        scope
    }

    /// Observes one executed Filter, Scorer, or Picker call using its compiled name.
    pub(crate) fn stage(
        &self,
        model_name: &str,
        round: RoutingStage,
        stage: &'static str,
        algorithm: &'static str,
        elapsed: Duration,
    ) {
        let labels = StageLabels {
            model_name: model_name.to_owned(),
            round: round_name(round),
            stage,
            algorithm,
        };
        if let Some(metric) = METRICS.stages.get(&labels) {
            metric.observe(elapsed.as_secs_f64());
        }
    }

    /// Observes rank-expanded candidate counts at the Router's eligibility boundaries.
    pub(crate) fn candidates(
        &self,
        model_name: &str,
        round: RoutingStage,
        stage: &'static str,
        count: usize,
    ) {
        let labels = CandidateLabels {
            model_name: model_name.to_owned(),
            round: round_name(round),
            stage,
        };
        if let Some(metric) = METRICS.candidates.get(&labels) {
            metric.observe(count as f64);
        }
    }

    /// Records one complete routing result and attributes successful choices to the selected target.
    pub(crate) fn selection(
        &self,
        model_name: &str,
        round: RoutingStage,
        elapsed: Duration,
        result: Result<&RouteCandidate, &RouteError>,
    ) {
        let outcome = match result {
            Ok(_) => "selected",
            Err(RouteError::NoMatchingRouteTarget { .. }) => "no_matching_target",
            Err(RouteError::NoMatchingDecode { .. }) => "no_matching_decode",
            Err(
                RouteError::InvalidFilterIndex { .. } | RouteError::DuplicateFilterIndex { .. },
            ) => "invalid_filter",
            Err(RouteError::InvalidScorerResult { .. }) => "invalid_scorer",
            Err(RouteError::EmptyPickerResult | RouteError::InvalidPickerIndex { .. }) => {
                "invalid_picker"
            }
            Err(RouteError::PrefillBeforeEncoder | RouteError::DecodeBeforePrefill) => {
                "invalid_sequence"
            }
        };
        let labels = OutcomeLabels {
            model_name: model_name.to_owned(),
            round: round_name(round),
            outcome,
        };
        if let Some(metric) = METRICS.selections.get(&labels) {
            metric.inc();
        }
        if let Some(metric) = METRICS.duration.get(&labels) {
            metric.observe(elapsed.as_secs_f64());
        }
        if let Ok(candidate) = result {
            let labels = TargetLabels {
                model_name: candidate.model.clone(),
                model_role: role_name(candidate.role),
                route_target_id: candidate.route_target_id.as_str().to_owned(),
                data_parallel_rank: candidate.data_parallel_rank,
            };
            if let Some(metric) = METRICS.target_selections.get(&labels) {
                metric.inc();
            }
        }
    }
}

impl Drop for RouterMetricsScope {
    fn drop(&mut self) {
        let mut references = METRICS
            .references
            .lock()
            .expect("router metric references lock poisoned");
        release_labels(&METRICS.stages, &mut references.stages, &self.stage_labels);
        let removed_outcomes = release_references(&mut references.outcomes, &self.outcome_labels);
        for labels in removed_outcomes {
            METRICS.selections.remove(&labels);
            METRICS.duration.remove(&labels);
        }
        release_labels(
            &METRICS.candidates,
            &mut references.candidates,
            &self.candidate_labels,
        );
        release_labels(
            &METRICS.target_selections,
            &mut references.targets,
            &self.target_labels,
        );
    }
}

fn retain_labels<S, M, C>(
    family: &Family<S, M, C>,
    references: &mut HashMap<S, usize>,
    labels: &[S],
) where
    S: Clone + Hash + Eq,
    C: MetricConstructor<M>,
{
    for labels in labels {
        let count = references.entry(labels.clone()).or_default();
        if *count == 0 {
            drop(family.get_or_create(labels));
        }
        *count += 1;
    }
}

fn release_labels<S, M, C>(
    family: &Family<S, M, C>,
    references: &mut HashMap<S, usize>,
    labels: &[S],
) where
    S: Clone + Hash + Eq,
    C: MetricConstructor<M>,
{
    for labels in release_references(references, labels) {
        family.remove(&labels);
    }
}

fn release_references<S>(references: &mut HashMap<S, usize>, labels: &[S]) -> Vec<S>
where
    S: Clone + Hash + Eq,
{
    let mut removed = Vec::new();
    for labels in labels {
        let count = references
            .get_mut(labels)
            .expect("router metric label scope must be registered");
        *count -= 1;
        if *count == 0 {
            references.remove(labels);
            removed.push(labels.clone());
        }
    }
    removed
}

fn round_name(round: RoutingStage) -> &'static str {
    match round {
        RoutingStage::Initial => "initial",
        RoutingStage::Prefill => "prefill",
        RoutingStage::Decode => "decode",
    }
}

fn role_name(role: ModelServerRole) -> &'static str {
    match role {
        ModelServerRole::Aggregate => "aggregate",
        ModelServerRole::Encoder => "encoder",
        ModelServerRole::Prefill => "prefill",
        ModelServerRole::Decode => "decode",
    }
}

pub(crate) static METRICS: LazyLock<RouterMetrics> = LazyLock::new(RouterMetrics::new);

/// Renders Router observations for the Frontend scrape handler, including the OpenMetrics EOF marker.
pub fn render_metrics() -> Result<String, fmt::Error> {
    let mut output = String::new();
    encode(&mut output, &METRICS.registry)?;
    Ok(output)
}
