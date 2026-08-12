// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Composable selection of one routable ModelGroup per routing round.

pub mod algorithm;
mod inventory;
mod request;
mod route_target_stats;
mod selection;

pub use algorithm::{KvLeastLoadedScorer, RouteFilter, RoutePicker, RouteScorer};
pub use inventory::{
    ModelRouteTable, RouteDecision, RouteInventory, RouteTarget, RouteTargetId, RouteTargetSet,
    ScalingTarget, ScalingTargetKind,
};
pub use request::RouterRequest;
pub use route_target_stats::{
    NoopRouteTargetStatsReader, RouteTargetLatencyStats, RouteTargetStats, RouteTargetStatsReader,
};
pub use selection::{
    FilterAlgorithm, PickerAlgorithm, PipelineRouter, RouteCandidate, RouteError, RouteScore,
    RouteSession, RouteTargetLoad, Router, RouterPipeline, RouterPipelineConfig, ScoredCandidate,
    ScorerAlgorithm,
};
