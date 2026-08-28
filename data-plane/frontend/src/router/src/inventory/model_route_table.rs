// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Model, token-limit, and capability matching over route targets.

use std::collections::BTreeSet;

use foretoken_engine_core_client::protocol::structured_outputs::StructuredOutputConstraint;

use crate::{RouteTarget, RouterRequest};

/// Immutable route targets matched by model and request requirements.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelRouteTable {
    routes: Vec<RouteTarget>,
}

impl ModelRouteTable {
    /// Creates a deterministically ordered table from static route targets.
    pub fn new(mut routes: Vec<RouteTarget>) -> Self {
        routes.sort_by(|a, b| a.route_target_id.cmp(&b.route_target_id));
        Self { routes }
    }

    /// Returns all static route targets in route-target identity order.
    pub fn routes(&self) -> &[RouteTarget] {
        &self.routes
    }

    /// Returns static routes whose model and input limit match one router request.
    ///
    /// `PipelineRouter` further applies dynamic health and capability checks; references remain owned by this table.
    pub(crate) fn candidates(&self, request: &RouterRequest) -> Vec<&RouteTarget> {
        self.routes
            .iter()
            .filter(|route| {
                route.ready
                    && route.model == request.model
                    && route
                        .max_input_tokens
                        .is_none_or(|limit| request.token_count() <= limit)
            })
            .collect()
    }
}

/// Checks whether a route target's trusted capabilities cover the request's optional features.
///
/// `PipelineRouter` consumes the boolean during candidate construction; neither input is retained.
pub(crate) fn supports_request(capabilities: &BTreeSet<String>, request: &RouterRequest) -> bool {
    (request.generate_request.lora_request.is_none() || capabilities.contains("lora"))
        && (request.generate_request.reasoning_parser_kwargs.is_none()
            || capabilities.contains("reasoning"))
        && supports_structured_output(capabilities, request)
        && supports_multimodal(capabilities, request)
}

fn supports_structured_output(capabilities: &BTreeSet<String>, request: &RouterRequest) -> bool {
    let Some(output) = &request.generate_request.sampling_params.structured_outputs else {
        return true;
    };
    let capability = match output.constraint {
        StructuredOutputConstraint::Json(_) => "structured_output.json_schema",
        StructuredOutputConstraint::JsonObject => "structured_output.json_object",
        StructuredOutputConstraint::Regex(_) => "structured_output.regex",
        StructuredOutputConstraint::Choice(_) => "structured_output.choice",
        StructuredOutputConstraint::Grammar(_) => "structured_output.grammar",
        StructuredOutputConstraint::StructuralTag(_) => "structured_output.structural_tag",
    };
    capabilities.contains(capability)
}

fn supports_multimodal(capabilities: &BTreeSet<String>, request: &RouterRequest) -> bool {
    let Some(features) = &request.generate_request.mm_features else {
        return true;
    };
    capabilities.contains("multimodal")
        && features
            .iter()
            .all(|feature| capabilities.contains(&format!("multimodal.{}", feature.modality)))
}
