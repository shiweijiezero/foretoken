// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scored-candidate selection and Picker implementations.

use crate::{CandidateIndex, RouterRequest, ScoredCandidate};

// Each entry declares the module, re-exports the implementation, and binds its user-facing Picker name.
// For example, `round_robin_picker => RoundRobinPicker = "round_robin"` maps
// `round_robin_picker.rs`, the `RoundRobinPicker` type, and the user-facing name.
declare_router_algorithms! {
    descriptor = PickerDescriptor;
    max_picker => MaxPicker = "max",
    round_robin_picker => RoundRobinPicker = "round_robin",
}

/// Selects one route target from the scored candidates available in the current routing stage.
///
/// A picker selects a position in `scored_candidates`; it cannot return or alter a candidate.
/// `None` is valid only when that slice is empty. The router reports an out-of-range index or an
/// empty result for a nonempty slice as a routing error.
///
/// - `request`: model, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `scored_candidates`: current-stage candidates with route target metadata and `RouteScore` locality
///   and load values.
/// - `customized_context`: user-defined `C`, created per request and shared by Prefill and Decode.
///
/// Returns the selected position in `scored_candidates`, or `None` when the list is empty.
pub trait RoutePicker<C: Send + 'static = ()>: Send + Sync {
    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        customized_context: &mut C,
    ) -> Option<CandidateIndex>;
}
