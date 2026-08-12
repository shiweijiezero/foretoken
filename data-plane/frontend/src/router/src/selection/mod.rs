// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Candidate construction and Filter-Scorer-Picker orchestration.

mod candidate;
mod config;
mod pipeline;
mod pipeline_router;
mod session;

pub use candidate::{RouteCandidate, RouteScore, RouteTargetLoad, ScoredCandidate};
pub use config::{FilterAlgorithm, PickerAlgorithm, RouterPipelineConfig, ScorerAlgorithm};
pub use pipeline::RouterPipeline;
pub use pipeline_router::PipelineRouter;
pub use session::{RouteError, RouteSession, Router};
