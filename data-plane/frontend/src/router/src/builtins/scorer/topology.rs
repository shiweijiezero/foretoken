// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use super::topology_priority;
use crate::{RouteContext, RouteOptionCandidate, RouteScore, RouteScorer};

/// 只保留拓扑优先级，使同分 option 由 Picker 轮转。
pub struct TopologyScorer;

impl RouteScorer for TopologyScorer {
    fn score(&self, option: &RouteOptionCandidate, _: RouteContext<'_>) -> RouteScore {
        RouteScore {
            topology: topology_priority(option.kind),
            locality: 0,
            load: 0,
        }
    }
}
