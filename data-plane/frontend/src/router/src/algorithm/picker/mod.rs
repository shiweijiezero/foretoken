// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Scored-candidate selection and Picker implementations.

use crate::{CandidateIndex, RouterRequest, RoutingProgress, ScoredCandidate};

// Each entry declares the module, re-exports the implementation, and binds its user-facing Picker name.
// For example, `gamble_sampling_picker => GambleSamplingPicker = "gamble_sampling"` maps
// `gamble_sampling_picker.rs`, the `GambleSamplingPicker` type, and the user-facing name.
declare_router_algorithms! {
    descriptor = PickerDescriptor;
    max_picker => MaxPicker = "max",
    power_of_two_choices_picker => PowerOfTwoChoicesPicker = "power_of_two_choices",
    gamble_sampling_picker => GambleSamplingPicker = "gamble_sampling",
}

/// Selects one route target from the scored candidates available in the current routing stage.
///
/// A picker selects a position in `scored_candidates`; it cannot return or alter a candidate.
/// `None` is valid only when that slice is empty. The router reports an out-of-range index or an
/// empty result for a nonempty slice as a routing error.
///
/// - `request`: model, prompt tokens, sampling, multimodal, LoRA, and priority.
/// - `scored_candidates`: current-stage candidates with route target metadata and numeric or
///   lexicographic `RouteScore` preferences.
/// - `routing_progress`: immutable E/P/D selection round and progress supplied by `RouteSession`.
/// - `customized_context`: user-defined `C`, created per request and shared across E/P/D rounds.
///
/// Returns the selected position in `scored_candidates`, or `None` when the list is empty.
pub trait RoutePicker<C: Send + 'static = ()>: Send + Sync {
    /// Applies algorithm-owned parameters during pipeline construction.
    fn configure(&mut self, parameters: serde_json::Value) -> Result<(), String> {
        if parameters
            .as_object()
            .is_some_and(|parameters| parameters.is_empty())
        {
            Ok(())
        } else {
            Err("this picker accepts no parameters".into())
        }
    }

    fn pick(
        &self,
        request: &RouterRequest,
        scored_candidates: &[ScoredCandidate],
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut C,
    ) -> Option<CandidateIndex>;
}
