// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Model, token-limit, and capability matching over route targets.

use std::collections::BTreeSet;

use foretoken_engine_core_client::protocol::structured_outputs::StructuredOutputConstraint;

use crate::{RouteTarget, RouteTargetId, RouterRequest};

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
    /// A retained Target remains eligible during drain for sessions already owning its KV.
    /// `PipelineRouter` applies dynamic health and capability checks; this table owns the results.
    pub(crate) fn candidates(
        &self,
        request: &RouterRequest,
        retained_target: Option<&RouteTargetId>,
    ) -> Vec<&RouteTarget> {
        self.routes
            .iter()
            .filter(|route| {
                (route.ready
                    || (route.role == foretoken_model_protocol::ModelServerRole::Target
                        && retained_target == Some(&route.route_target_id)))
                    && route.model == request.model
                    && (request.allowed_pools.is_empty()
                        || request.allowed_pools.contains(&route.target.name))
                    && route
                        .max_input_tokens
                        .is_none_or(|limit| request.input_tokens <= limit)
            })
            .collect()
    }
}

/// Checks whether a route target's trusted capabilities cover the request's optional features.
///
/// `PipelineRouter` consumes the boolean during candidate construction; neither input is retained.
pub(crate) fn supports_request(
    capabilities: &BTreeSet<String>,
    request: &RouterRequest,
    requires_encoder: bool,
) -> bool {
    let Some(text) = &request.generate_request else {
        return capabilities.contains("video");
    };
    if capabilities.contains("video") {
        return false;
    }
    (text.lora_request.is_none() || capabilities.contains("lora"))
        && supports_structured_output(capabilities, request)
        && (!requires_encoder || supports_multimodal(capabilities, request))
}

fn supports_structured_output(capabilities: &BTreeSet<String>, request: &RouterRequest) -> bool {
    let Some(output) = request
        .generate_request
        .as_ref()
        .and_then(|text| text.sampling_params.structured_outputs.as_ref())
    else {
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
    let Some(features) = request
        .generate_request
        .as_ref()
        .and_then(|text| text.mm_features.as_ref())
    else {
        return true;
    };
    capabilities.contains("multimodal")
        && features
            .iter()
            .all(|feature| capabilities.contains(&format!("multimodal.{}", feature.modality)))
}
