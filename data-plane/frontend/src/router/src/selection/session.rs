// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request-local routing session contract and selection errors.

use thiserror::Error;

use crate::{RouteDecision, RouterRequest};

/// Aggregate completes directly; P/D executes P→fresh D and E/P/D executes E→P→fresh D.
pub trait RouteSession: Send {
    /// Selects one Aggregate, ordinary Prefill, or E/P/D Encoder from the current snapshot.
    fn select_initial(&mut self) -> Result<RouteDecision, RouteError>;

    /// Selects the Prefill in the Encoder-selected E/P/D domain.
    fn select_prefill(&mut self) -> Result<RouteDecision, RouteError>;

    /// Selects one Decode route target from a fresh snapshot after Prefill completes.
    fn select_decode(&mut self) -> Result<RouteDecision, RouteError>;
}

/// Creates isolated routing sessions for tokenized generation requests.
pub trait Router: Send + Sync {
    /// Starts one request-local routing session.
    fn start(&self, request: RouterRequest) -> Box<dyn RouteSession>;
}

/// Failure returned while selecting one route target execution stage.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum RouteError {
    /// No ready Aggregate or Prefill route target matches the request.
    #[error("no ready route target matches model {model}")]
    NoMatchingRouteTarget {
        /// Requested logical model.
        model: String,
    },
    /// No ready Decode route target matches the request.
    #[error("no decode route target matches model {model}")]
    NoMatchingDecode {
        /// Requested logical model.
        model: String,
    },
    /// Picker returned a route target outside the scored candidates for this stage.
    #[error("route picker returned no candidate from this selection round")]
    InvalidPickerResult,
    /// Prefill selection was requested before an Encoder route target was selected.
    #[error("prefill selection requires a selected encoder route target")]
    PrefillBeforeEncoder,
    /// Decode selection was requested before a Prefill route target was selected.
    #[error("decode selection requires a selected prefill route target")]
    DecodeBeforePrefill,
}
