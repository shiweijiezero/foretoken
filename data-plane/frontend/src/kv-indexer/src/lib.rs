// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Controller-owned, best-effort, typed KV locality indexing.
mod config;
mod index;
mod sync;
pub use config::*;
pub use index::positional_hash::PositionalHashIndex;
pub use index::radix_tree::RadixTreeIndex;
pub use index::*;
pub use sync::*;

/// Deterministic prefix facts for one exact route-to-source binding.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct KvPrefixMatches(Vec<KvPrefixMatch>);

impl KvPrefixMatches {
    /// Sorts facts into a stable order before exposing them to a scorer.
    pub fn new(mut matches: Vec<KvPrefixMatch>) -> Self {
        matches.sort();
        Self(matches)
    }
}

impl IntoIterator for KvPrefixMatches {
    type Item = KvPrefixMatch;
    type IntoIter = std::vec::IntoIter<KvPrefixMatch>;

    fn into_iter(self) -> Self::IntoIter {
        self.0.into_iter()
    }
}

/// Borrowed inputs for one route- and rank-specific prefix lookup.
#[derive(Debug, Clone, Copy)]
pub struct KvPrefixLookup<'a> {
    /// Routable ModelGroup identity used to resolve the event-source binding.
    pub route_target_id: &'a str,
    /// Exact data-parallel replica represented by the routing candidate.
    pub data_parallel_rank: u32,
    /// Prompt tokens hashed by the selected locality index.
    pub prompt_token_ids: &'a [u32],
}

impl<'a> KvPrefixLookup<'a> {
    pub const fn new(
        route_target_id: &'a str,
        data_parallel_rank: u32,
        prompt_token_ids: &'a [u32],
    ) -> Self {
        Self {
            route_target_id,
            data_parallel_rank,
            prompt_token_ids,
        }
    }
}

/// Route-local typed facts. `Unavailable` is fail-closed: it is not a confirmed cache miss.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KvPrefixQueryResult {
    Matches(KvPrefixMatches),
    Unavailable(KvPrefixUnavailableReason),
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum KvPrefixUnavailableReason {
    Disabled,
    SourceUnhealthy,
    MissingBinding,
    UnsupportedRequest,
    RankMismatch,
}
pub trait KvPrefixIndexer: Send + Sync {
    /// Looks up confirmed prefix-locality facts for one route target and data-parallel rank.
    ///
    /// Router filters and scorers consume the derived result; implementations retain ownership of their index state.
    fn prefix_matches(&self, lookup: KvPrefixLookup<'_>) -> KvPrefixQueryResult;
}
pub struct NoopKvPrefixIndexer;
impl KvPrefixIndexer for NoopKvPrefixIndexer {
    fn prefix_matches(&self, _: KvPrefixLookup<'_>) -> KvPrefixQueryResult {
        KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::Disabled)
    }
}
