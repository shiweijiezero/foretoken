// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::Arc;

use super::{topology_priority, total_load};
use crate::{KvPrefixScorer, RouteContext, RouteOptionCandidate, RouteScore, RouteScorer};

/// Default scorer: topology precedence, prefill KV locality, then total load.
pub struct KvLoadScorer {
    prefix: Arc<dyn KvPrefixScorer>,
}

impl KvLoadScorer {
    pub fn new(prefix: Arc<dyn KvPrefixScorer>) -> Self {
        Self { prefix }
    }
}

impl RouteScorer for KvLoadScorer {
    fn score(&self, option: &RouteOptionCandidate, context: RouteContext<'_>) -> RouteScore {
        let topology = topology_priority(option.kind);
        let locality = option
            .components
            .iter()
            .find(|component| component.uses_prefix_locality)
            .map_or(0, |component| {
                self.prefix
                    .score_prefill_prefix(&component.backend_id, context)
            });
        RouteScore {
            topology,
            locality: i64::try_from(locality).unwrap_or(i64::MAX),
            load: -total_load(option),
        }
    }
}
