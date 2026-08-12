// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Composable Filter-Scorer-Picker pipeline and request context factory.

use std::sync::Arc;

use crate::{RouteFilter, RoutePicker, RouteScorer, RouterRequest};

/// Filter, Scorer, Picker, and per-request customized context factory.
pub struct RouterPipeline<C: Send + 'static = ()> {
    /// Candidate-list filter.
    pub(super) filter: Arc<dyn RouteFilter<C>>,
    /// Filtered-candidate scorer.
    pub(super) scorer: Arc<dyn RouteScorer<C>>,
    /// Final scored-candidate picker.
    pub(super) picker: Arc<dyn RoutePicker<C>>,
    /// Creates isolated algorithm context for each request.
    pub(super) customized_context_factory: Arc<dyn Fn(&RouterRequest) -> C + Send + Sync>,
}
impl RouterPipeline<()> {
    /// Creates a pipeline whose algorithms need no custom per-request context.
    pub fn new(
        filter: Arc<dyn RouteFilter>,
        scorer: Arc<dyn RouteScorer>,
        picker: Arc<dyn RoutePicker>,
    ) -> Self {
        Self::with_customized_context(filter, scorer, picker, |_| ())
    }
}
impl<C: Send + 'static> RouterPipeline<C> {
    /// Creates a pipeline and a factory for context shared across one request's routing rounds.
    pub fn with_customized_context(
        filter: Arc<dyn RouteFilter<C>>,
        scorer: Arc<dyn RouteScorer<C>>,
        picker: Arc<dyn RoutePicker<C>>,
        customized_context_factory: impl Fn(&RouterRequest) -> C + Send + Sync + 'static,
    ) -> Self {
        Self {
            filter,
            scorer,
            picker,
            customized_context_factory: Arc::new(customized_context_factory),
        }
    }
}
