// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use crate::{RouteContext, RouteOptionCandidate, RouteOptionKind, RouteScore, RouteScorer};

/// 保留 Foretoken 的拓扑优先级，并在同类拓扑中偏好总负载更低的选项。
pub struct SimpleScorer;

impl RouteScorer for SimpleScorer {
    fn score(&self, option: &RouteOptionCandidate, _: RouteContext<'_>) -> RouteScore {
        let topology = match option.kind {
            RouteOptionKind::Aggregate => 2,
            RouteOptionKind::EncoderPrefillDecode => 1,
            RouteOptionKind::PrefillDecode => 0,
        };
        let total_load = option
            .components
            .iter()
            .fold(0_u64, |sum, component| sum.saturating_add(component.load));
        RouteScore {
            topology,
            locality: 0,
            load: -i64::try_from(total_load).unwrap_or(i64::MAX),
        }
    }
}
