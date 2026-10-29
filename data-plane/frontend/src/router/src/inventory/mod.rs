// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Static route targets and request matching.

use crate::RouteTargetId;
use std::collections::BTreeSet;

mod model_route_table;
mod route;

pub use model_route_table::ModelRouteTable;
pub(crate) use model_route_table::supports_request;
pub use route::RouteDecision;

/// Supplies static routes and current route target state to the Router.
pub trait RouteInventory: Send + Sync {
    /// Returns the immutable routes available to Router matching.
    fn model_routes(&self) -> &ModelRouteTable;
    /// Reports whether one route target may receive new work.
    fn is_route_target_healthy(&self, route_target_id: &RouteTargetId) -> bool;

    /// Resolves the HTTP endpoint of a selected aggregate route; selection stays with the Router.
    fn http_endpoint(&self, _decision: &RouteDecision) -> Option<String> {
        None
    }

    /// Returns observed KV transfer settings for Decode source discovery.
    /// Missing metadata leaves source discovery disabled.
    fn kv_transfer(
        &self,
        _route_target_id: &RouteTargetId,
    ) -> Option<foretoken_model_protocol::RuntimeKvTransferMetadata> {
        None
    }

    /// Returns the engine's KV allocation block size for routing cost estimates.
    fn kv_block_size(&self, _route_target_id: &RouteTargetId) -> Option<std::num::NonZeroU32> {
        None
    }

    /// Reports whether the engine shares prefix-cache allocations between requests.
    fn prefix_cache_enabled(&self, _route_target_id: &RouteTargetId) -> Option<bool> {
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
