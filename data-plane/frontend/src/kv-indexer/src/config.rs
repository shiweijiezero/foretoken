// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Controller-owned index choice with explainable Auto resolution.
use serde::{Deserialize, Serialize};
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum KvLocalityIndexImplementation {
    #[default]
    Auto,
    PositionalHash,
    RadixTree,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub enum KvLocalityIndexResolvedImplementation {
    PositionalHash,
    RadixTree,
}
/// Concrete topology fact that selected the Auto implementation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KvAutoResolutionReason {
    NoEventSources,
    SingleSourcePrimaryRank,
    IndependentPrimarySources,
    MultipleSourcesInScope,
    NonPrimaryDataParallelRank,
    ReadableTierContinuation,
}
/// Observed ownership and capability facts used by Auto; it carries no tuning thresholds.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct KvLocalityTopology {
    pub(crate) event_source_count: usize,
    pub(crate) has_scope_with_distinct_sources: bool,
    pub(crate) has_non_primary_data_parallel_rank: bool,
    pub(crate) has_readable_tier_continuation: bool,
}
impl KvLocalityTopology {
    pub(crate) fn resolution_reason(&self) -> KvAutoResolutionReason {
        if self.event_source_count == 0 {
            KvAutoResolutionReason::NoEventSources
        } else if self.has_non_primary_data_parallel_rank {
            KvAutoResolutionReason::NonPrimaryDataParallelRank
        } else if self.has_readable_tier_continuation {
            KvAutoResolutionReason::ReadableTierContinuation
        } else if self.has_scope_with_distinct_sources {
            KvAutoResolutionReason::MultipleSourcesInScope
        } else if self.event_source_count > 1 {
            KvAutoResolutionReason::IndependentPrimarySources
        } else {
            KvAutoResolutionReason::SingleSourcePrimaryRank
        }
    }
}
impl KvLocalityIndexImplementation {
    pub(crate) fn resolve(self, t: &KvLocalityTopology) -> KvLocalityIndexResolvedImplementation {
        match self {
            Self::RadixTree => KvLocalityIndexResolvedImplementation::RadixTree,
            Self::PositionalHash => KvLocalityIndexResolvedImplementation::PositionalHash,
            Self::Auto
                if matches!(
                    t.resolution_reason(),
                    KvAutoResolutionReason::NoEventSources
                        | KvAutoResolutionReason::SingleSourcePrimaryRank
                        | KvAutoResolutionReason::IndependentPrimarySources
                ) =>
            {
                KvLocalityIndexResolvedImplementation::PositionalHash
            }
            Self::Auto => KvLocalityIndexResolvedImplementation::RadixTree,
        }
    }
}
