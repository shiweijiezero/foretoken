// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Composable selection of one routable ModelGroup per routing round.

pub mod algorithm;
mod cache;
mod inventory;
mod metrics;
mod request;
mod route_target_stats;
mod routing_load;
pub use foretoken_serving_types::{
    RouteTarget, RouteTargetId, RouteTargetLatencyStats, RouteTargetSet, RouteTargetStats,
    RoutingLoadSnapshot, ScalingTarget, ScalingTargetKind,
};
pub use routing_load::RoutingLoadState;
mod selection;

pub use algorithm::{
    GambleSamplingPicker, KvLeastLoadedScorer, PowerOfTwoChoicesPicker, RouteFilter, RoutePicker,
    RouteScorer,
};
pub use inventory::{ModelRouteTable, RouteDecision, RouteInventory};
pub use metrics::render_metrics;
pub use request::RouterRequest;
pub use route_target_stats::RouteTargetStatsReader;
pub use selection::{
    AlgorithmName, CandidateIndex, FilterAlgorithm, FilterDescriptor, FilterStage, PickerAlgorithm,
    PickerDescriptor, PickerStage, PipelineRouter, RouteCandidate, RouteError, RouteScore,
    RouteSession, Router, RouterPipeline, RouterPipelineConfig, RouterPipelineConfigError,
    RoutingProgress, RoutingStage, ScoredCandidate, ScorerAlgorithm, ScorerDescriptor, ScorerStage,
};
