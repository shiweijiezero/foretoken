// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Static route targets and request matching.

use std::collections::BTreeSet;

mod model_route_table;
mod route;

pub use model_route_table::ModelRouteTable;
pub(crate) use model_route_table::supports_request;
pub use route::{
    RouteDecision, RouteTarget, RouteTargetId, RouteTargetSet, ScalingTarget, ScalingTargetKind,
};

/// Supplies static routes and current route target state to the Router.
pub trait RouteInventory: Send + Sync {
    /// Returns the immutable routes available to Router matching.
    fn model_routes(&self) -> &ModelRouteTable;
    /// Reports whether one route target may receive new work.
    fn is_route_target_healthy(&self, route_target_id: &RouteTargetId) -> bool;

    /// Returns the latest current-load snapshot when available.
    #[allow(unused_variables)]
    fn route_target_load(&self, route_target_id: &RouteTargetId) -> Option<crate::RouteTargetLoad> {
        None
    }

    /// Returns the capabilities currently trusted for one route target.
    fn effective_capabilities(&self, route_target_id: &RouteTargetId) -> BTreeSet<String> {
        self.model_routes()
            .routes()
            .iter()
            .find(|route| &route.route_target_id == route_target_id)
            .map(|route| route.capabilities.clone())
            .unwrap_or_default()
    }
}
