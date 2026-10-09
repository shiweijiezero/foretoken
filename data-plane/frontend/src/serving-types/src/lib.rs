// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared serving identities and read-only observations for frontend consumers.

mod load;
mod statistics;
mod target;

pub use load::RoutingLoadSnapshot;
pub use statistics::{RouteTargetLatencyStats, RouteTargetStats};
pub use target::{RouteTarget, RouteTargetId, RouteTargetSet, ScalingTarget, ScalingTargetKind};
