// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

mod kv_load;
mod least_loaded;
mod topology;

use crate::{RouteOptionCandidate, RouteOptionKind};

pub use kv_load::KvLoadScorer;
pub use least_loaded::LeastLoadedScorer;
pub use topology::TopologyScorer;

fn topology_priority(kind: RouteOptionKind) -> i64 {
    match kind {
        RouteOptionKind::Aggregate => 2,
        RouteOptionKind::EncoderPrefillDecode => 1,
        RouteOptionKind::PrefillDecode => 0,
    }
}

fn total_load(option: &RouteOptionCandidate) -> i64 {
    let load = option
        .components
        .iter()
        .fold(0_u64, |sum, component| sum.saturating_add(component.load));
    i64::try_from(load).unwrap_or(i64::MAX)
}
