// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Trusted service constraints and live, read-only observations for admission rules.

use std::collections::BTreeMap;
use std::time::{Duration, Instant};

use foretoken_serving_types::{RouteTarget, RouteTargetStats, RoutingLoadSnapshot};

/// Identity resolved by a trusted authentication boundary, never inferred from client hints.
#[derive(Clone, Debug)]
pub struct AdmissionIdentity {
    pub tenant: String,
    pub subject: Option<String>,
}

/// Configured latency objectives, distinct from the request's execution deadline.
#[derive(Clone, Copy, Debug, Default)]
pub struct AdmissionObjectives {
    /// Target from frontend processing origin to the first output token.
    pub time_to_first_token: Option<Duration>,
    /// Target average decode time per output token after the first token.
    pub time_per_output_token: Option<Duration>,
    /// Target from frontend processing origin to request completion, not an execution timeout.
    pub completion_latency: Option<Duration>,
}

/// Resolved service policy. Missing identity or policy remains absent, not an invented default tier.
#[derive(Clone, Debug, Default)]
pub struct AdmissionService {
    pub identity: Option<AdmissionIdentity>,
    pub class: Option<String>,
    /// Configured admission priority; higher values take precedence when a rule uses priority.
    pub priority: Option<i32>,
    pub objectives: Option<AdmissionObjectives>,
}

/// Current model availability from the serving-generation owner.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionModelStatus {
    Unknown,
    Preparing,
    Ready,
    Unavailable,
}

/// Metadata and cached load for one target; none of these observations reserve backend capacity.
#[derive(Clone, Debug)]
pub struct AdmissionTargetState {
    pub target: RouteTarget,
    pub healthy: bool,
    pub statistics: Option<RouteTargetStats>,
    /// Reservations made by this frontend, kept separate from engine telemetry.
    pub frontend_load: BTreeMap<u32, RoutingLoadSnapshot>,
}

/// A lightweight observation, not a retained tokenizer, backend connection, or serving runtime.
#[derive(Clone, Debug)]
pub struct AdmissionModelState {
    pub status: AdmissionModelStatus,
    /// Context-length limit from the prepared model processor, absent until it is available.
    pub max_model_len: Option<u32>,
    pub targets: Vec<AdmissionTargetState>,
    /// Time this view was sampled; target statistics retain their own collection timestamps.
    pub observed_at: Instant,
}

/// Reads the current serving generation and locally cached telemetry without request-path I/O.
pub trait AdmissionStateReader: Send + Sync {
    /// Returns a fresh model view over the requested statistics window, or None before publication.
    fn model_state(&self, model: &str, window: Duration) -> Option<AdmissionModelState>;
}

/// Framework-provided deadline, trusted policy, and live state access for one admission attempt.
/// Rules may query observations again after waiting; the context never pins an old generation.
pub struct AdmissionContext<'a> {
    pub deadline: tokio::time::Instant,
    pub service: AdmissionService,
    pub state: &'a dyn AdmissionStateReader,
    /// Queue timing supplied by the framework; rules keep capacity and scheduling ownership.
    pub queue: super::AdmissionQueueObservation,
    /// Counters for resources reserved by this model's rule.
    pub metrics: super::AdmissionMetricsHandle,
}
