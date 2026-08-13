// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request-local routing state behavior and selection errors.

use thiserror::Error;

use crate::{RouteDecision, RouterRequest};

/// Holds request-local routing state for one generation request. Aggregate completes directly;
/// P/D executes P→a fresh D choice, and E/P/D executes E→P→a fresh D choice within one linked route set.
pub trait RouteSession: Send {
    /// Selects one Aggregate, ordinary Prefill, or E/P/D Encoder from the current snapshot.
    fn select_initial(&mut self) -> Result<RouteDecision, RouteError>;

    /// Selects the Prefill in the Encoder-selected E/P/D linked route set.
    fn select_prefill(&mut self) -> Result<RouteDecision, RouteError>;

    /// Selects one Decode model-server route from a fresh snapshot after Prefill completes.
    fn select_decode(&mut self) -> Result<RouteDecision, RouteError>;
}

/// Creates isolated request-local routing state for tokenized generation requests.
pub trait Router: Send + Sync {
    /// Starts routing state for one generation request.
    fn start(&self, request: RouterRequest) -> Box<dyn RouteSession>;
}

/// Failure returned while selecting one model-server routing stage.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum RouteError {
    /// No ready Aggregate or Prefill model-server route matches the request.
    #[error("no ready model server route matches model {model}")]
    NoMatchingRouteTarget {
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
