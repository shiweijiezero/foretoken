// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request-local routing state behavior and selection errors.

use foretoken_model_protocol::ModelServerRole;
use thiserror::Error;

use crate::{RouteDecision, RouterRequest};

/// Identifies the selection round visible to routing algorithms.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RoutingStage {
    /// Selects the first route, which may be Aggregate, Prefill, Encoder, or Target.
    Initial,
    /// Selects Prefill after an Encoder when the selected topology requires it.
    Prefill,
    /// Selects Decode after Prefill.
    Decode,
    /// Selects a Draft while retaining the selected Target reservation.
    Draft,
}

/// Immutable split-workflow routing progress visible to algorithms for one selection round.
///
/// `RouteSession` owns and constructs this view; Filter, Scorer, and Picker may read it but cannot
/// mutate routing progress or treat it as client input.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RoutingProgress<'a> {
    /// Serving-snapshot version retained by this session; zero denotes an unversioned inventory.
    /// Shared algorithm state must not let older snapshots supersede newer observations.
    pub snapshot_version: u64,
    /// Selection round currently being executed.
    pub current_stage: RoutingStage,
    /// Execution roles completed before this selection round.
    pub completed_stages: &'a [ModelServerRole],
    /// E/P/D route-set identity bound by an earlier selection, if any.
    pub pipeline_scope_id: Option<&'a str>,
}

/// Holds request-local routing state for one generation request. Aggregate completes directly;
/// P/D and E/P/D select sequential stages. DT uses independently reserved batch rounds.
pub trait RouteSession: Send {
    /// Releases prompt-token load when the first response reaches the frontend.
    fn response_started(&mut self) {}

    /// Releases the completed stage's load; dropping the session also releases outstanding stages.
    fn stage_complete(&mut self) {}

    /// Selects one Aggregate, Prefill, Encoder, or Target from the current snapshot.
    fn select_initial(&mut self) -> Result<RouteDecision, RouteError>;

    /// Selects the Prefill in the Encoder-selected E/P/D route set.
    fn select_prefill(&mut self) -> Result<RouteDecision, RouteError>;

    /// Selects one Decode model-server route from a fresh snapshot after Prefill completes.
    fn select_decode(&mut self) -> Result<RouteDecision, RouteError>;
}

/// Creates isolated request-local routing state for tokenized generation requests.
#[async_trait::async_trait]
pub trait Router: Send + Sync {
    /// Binds a prepared request to fresh routing observations and retains its routing load.
    async fn start(&self, request: RouterRequest) -> Box<dyn RouteSession>;

    /// Selects compatible DT workers for one round and reserves its members.
    /// The baseline uses the first member's placement preference, with compatibility checked
    /// for every member. The executor supplies Target affinity and known closed workers;
    /// Router does not infer migration support or replay failed work.
    /// The returned owner releases reservations as members return or the round drops.
    async fn route_draft_target_round(
        &self,
        _requests: &[RouterRequest],
        _placement: &crate::DraftTargetPlacement,
    ) -> Result<crate::DraftTargetRoundRoute, RouteError> {
        Err(RouteError::BatchRoutingUnsupported)
    }
}

/// Failure returned while selecting one model-server routing stage.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum RouteError {
    /// Compatible execution paths exist, but policy filtering temporarily excludes them.
    #[error("compatible model servers are temporarily busy")]
    Busy,
    /// A batch must contain distinct generation requests for one logical model.
    #[error("DT batch requires distinct generation requests for one model")]
    InvalidBatch,
    /// This router has no batch placement implementation.
    #[error("this router does not support DT batch placement")]
    BatchRoutingUnsupported,
    /// No ready Aggregate or Prefill model-server route matches the request.
    #[error("no ready model server route matches model {model}")]
    NoCompatibleTarget {
        /// Requested logical model.
        model: String,
    },
    /// No ready Decode model-server route matches the request.
    #[error("no decode model server route matches model {model}")]
    NoMatchingDecode {
        /// Requested logical model.
        model: String,
    },
    /// Filter selected a position that is not present in its input snapshot.
    #[error("route filter selected out-of-range candidate index {index}")]
    InvalidFilterIndex { index: usize },
    /// Filter selected a position more than once.
    #[error("route filter selected candidate index {index} more than once")]
    DuplicateFilterIndex { index: usize },
    /// Scorer did not return exactly one score for each filtered candidate.
    #[error("route scorer returned {actual} scores for {expected} candidates")]
    InvalidScorerResult { expected: usize, actual: usize },
    /// Picker returned no selection despite having candidates available.
    #[error("route picker returned no candidate from a nonempty selection round")]
    EmptyPickerResult,
    /// Picker selected a position that is not present in its current scored slice.
    #[error("route picker selected out-of-range candidate index {index}")]
    InvalidPickerIndex { index: usize },
    /// Prefill selection was requested before an Encoder model-server route was selected.
    #[error("prefill selection requires a selected encoder model server route")]
    PrefillBeforeEncoder,
    /// Decode selection was requested before a Prefill model-server route was selected.
    #[error("decode selection requires a selected prefill model server route")]
    DecodeBeforePrefill,
}
