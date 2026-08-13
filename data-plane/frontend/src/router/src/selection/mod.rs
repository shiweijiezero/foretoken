// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Candidate construction and Filter-Scorer-Picker orchestration.

mod candidate;
mod config;
mod pipeline;
mod pipeline_router;
mod session;

pub use candidate::{CandidateIndex, RouteCandidate, RouteScore, ScoredCandidate};
pub use config::{
    AlgorithmName, FilterAlgorithm, FilterDescriptor, PickerAlgorithm, PickerDescriptor,
    RouterPipelineConfig, RouterPipelineConfigError, ScorerAlgorithm, ScorerDescriptor,
    validate_descriptor_names,
};
pub use pipeline::RouterPipeline;
pub use pipeline_router::PipelineRouter;
pub use session::{RouteError, RouteSession, Router};
