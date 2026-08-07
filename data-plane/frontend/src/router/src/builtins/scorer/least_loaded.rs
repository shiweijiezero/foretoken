// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use super::{topology_priority, total_load};
use crate::{RouteContext, RouteOptionCandidate, RouteScore, RouteScorer};

/// 在同类拓扑中优先选择总负载更低的 option。
pub struct LeastLoadedScorer;

impl RouteScorer for LeastLoadedScorer {
    fn score(&self, option: &RouteOptionCandidate, _: RouteContext<'_>) -> RouteScore {
        RouteScore {
            topology: topology_priority(option.kind),
            locality: 0,
            load: -total_load(option),
        }
    }
}
