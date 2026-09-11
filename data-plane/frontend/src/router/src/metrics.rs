// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Process-local routing observations, exported through the Frontend OpenMetrics endpoint.

use std::fmt;
use std::sync::LazyLock;
use std::time::Duration;

use prometheus_client::encoding::EncodeLabelSet;
use prometheus_client::encoding::text::encode;
use prometheus_client::metrics::counter::Counter;
use prometheus_client::metrics::family::Family;
use prometheus_client::metrics::histogram::{Histogram, exponential_buckets};
use prometheus_client::registry::Registry;

use crate::{RouteError, RoutingStage};

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct StageLabels {
    round: &'static str,
    stage: &'static str,
    algorithm: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct OutcomeLabels {
    round: &'static str,
    outcome: &'static str,
}

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct CandidateLabels {
    round: &'static str,
    stage: &'static str,
}

pub(crate) struct RouterMetrics {
    registry: Registry,
    stages: Family<StageLabels, Histogram, fn() -> Histogram>,
    selections: Family<OutcomeLabels, Counter>,
    duration: Family<OutcomeLabels, Histogram, fn() -> Histogram>,
    candidates: Family<CandidateLabels, Histogram, fn() -> Histogram>,
}

fn latency_histogram() -> Histogram {
    Histogram::new(exponential_buckets(0.00001, 4.0, 10))
}

fn candidate_histogram() -> Histogram {
    Histogram::new(exponential_buckets(1.0, 2.0, 12))
}

impl RouterMetrics {
    // The registry and its handles share one process lifetime. Labels contain only compiled
    // algorithm names and fixed outcomes, never request IDs, model names, or candidate identities.
    fn new() -> Self {
        let mut registry = Registry::default();
        let stages = Family::new_with_constructor(latency_histogram as fn() -> Histogram);
        let selections = Family::default();
        let duration = Family::new_with_constructor(latency_histogram as fn() -> Histogram);
        let candidates = Family::new_with_constructor(candidate_histogram as fn() -> Histogram);
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
        Self {
            registry,
            stages,
            selections,
            duration,
            candidates,
        }
    }

    /// Observes one executed Filter, Scorer, or Picker call using its compiled name.
    pub(crate) fn stage(
        &self,
        round: RoutingStage,
        stage: &'static str,
        algorithm: &'static str,
        elapsed: Duration,
    ) {
        self.stages
            .get_or_create(&StageLabels {
                round: round_name(round),
                stage,
                algorithm,
            })
            .observe(elapsed.as_secs_f64());
    }

    /// Observes rank-expanded candidate counts at the Router's eligibility boundaries.
    pub(crate) fn candidates(&self, round: RoutingStage, stage: &'static str, count: usize) {
        self.candidates
            .get_or_create(&CandidateLabels {
                round: round_name(round),
                stage,
            })
            .observe(count as f64);
    }

    /// Records a complete selection result without exposing request-dependent error text as labels.
    pub(crate) fn selection(
        &self,
        round: RoutingStage,
        elapsed: Duration,
        error: Option<&RouteError>,
    ) {
        let outcome = match error {
            None => "selected",
            Some(RouteError::NoMatchingRouteTarget { .. }) => "no_matching_target",
            Some(RouteError::NoMatchingDecode { .. }) => "no_matching_decode",
            Some(
                RouteError::InvalidFilterIndex { .. } | RouteError::DuplicateFilterIndex { .. },
            ) => "invalid_filter",
            Some(RouteError::InvalidScorerResult { .. }) => "invalid_scorer",
            Some(RouteError::EmptyPickerResult | RouteError::InvalidPickerIndex { .. }) => {
                "invalid_picker"
            }
            Some(RouteError::PrefillBeforeEncoder | RouteError::DecodeBeforePrefill) => {
                "invalid_sequence"
            }
        };
        let labels = OutcomeLabels {
            round: round_name(round),
            outcome,
        };
        self.selections.get_or_create(&labels).inc();
        self.duration
            .get_or_create(&labels)
            .observe(elapsed.as_secs_f64());
    }
}

fn round_name(round: RoutingStage) -> &'static str {
    match round {
        RoutingStage::Initial => "initial",
        RoutingStage::Prefill => "prefill",
        RoutingStage::Decode => "decode",
    }
}

pub(crate) static METRICS: LazyLock<RouterMetrics> = LazyLock::new(RouterMetrics::new);

/// Renders Router observations for the Frontend scrape handler, including the OpenMetrics EOF marker.
pub fn render_metrics() -> Result<String, fmt::Error> {
    let mut output = String::new();
    encode(&mut output, &METRICS.registry)?;
    Ok(output)
}
