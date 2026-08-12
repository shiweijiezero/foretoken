// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scored-candidate selection contract and built-in pickers.

mod max_picker;
mod round_robin_picker;

use crate::{RouteCandidate, RouterRequest, ScoredCandidate};

pub use max_picker::MaxPicker;
pub use round_robin_picker::RoundRobinPicker;

/// Selects one route target from the scored candidates available in the current routing stage.
///
/// A picker must return an unmodified candidate from `scored_candidates`; it must return `None`
/// only when that slice is empty. The router rejects any result outside this scored set.
///
/// - `request`: model, optional revision, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `scored_candidates`: current-stage candidates with route target metadata and `RouteScore` locality
///   and load values.
/// - `customized_context`: user-defined `C`, created per request and shared by Prefill and Decode.
///
/// Returns one candidate from `scored_candidates`, or `None` when the list is empty.
pub trait RoutePicker<C: Send + 'static = ()>: Send + Sync {
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        customized_context: &mut C,
    ) -> Option<RouteCandidate>;
}
