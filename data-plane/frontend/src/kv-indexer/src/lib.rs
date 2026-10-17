// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Frontend-owned KV locality indexing and request-specific prefix observations.
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
    /// Keeps positive prefix matches in a stable order for routing consumers.
    pub fn new(mut matches: Vec<KvPrefixMatch>) -> Self {
        matches.retain(|hit| hit.matched_tokens > 0);
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
#[derive(Debug, Clone)]
pub struct KvPrefixLookup<'a> {
    /// Routing identity used to resolve the candidate's cache observation source.
    pub route_target_id: &'a str,
    /// Exact data-parallel replica represented by the routing candidate.
    pub data_parallel_rank: u32,
    /// Processed prompt tokens used for cache lookup.
    pub prompt_token_ids: &'a [u32],
    /// Cache identity already attached to the generation request; not an authentication claim.
    pub cache_salt: Option<&'a str>,
    pub lora_name: Option<&'a str>,
    pub lora_path: Option<&'a str>,
    pub multimodal: Vec<foretoken_model_protocol::KvMultimodalIdentity>,
}

impl<'a> KvPrefixLookup<'a> {
    /// Copies the processed cache identity into the model-server observation request.
    pub(crate) fn to_request(&self) -> foretoken_model_protocol::KvPrefixRequest {
        foretoken_model_protocol::KvPrefixRequest {
            prompt_token_ids: self.prompt_token_ids.to_vec(),
            dp_rank: self.data_parallel_rank,
            cache_salt: self.cache_salt.map(str::to_owned),
            lora_name: self.lora_name.map(str::to_owned),
            lora_path: self.lora_path.map(str::to_owned),
            multimodal: self.multimodal.clone(),
        }
    }
}

/// Route-local prefix observations; an unavailable result is distinct from a confirmed miss.
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
    UnsupportedLayout,
    UnsupportedRequest,
    RankMismatch,
}
#[async_trait::async_trait]
pub trait KvPrefixIndexer: Send + Sync {
    /// Prepares request-local observations from engines and connectors before synchronous scoring.
    /// Event-only implementations may retain their existing reader.
    async fn prepare(
        &self,
        _lookups: &[KvPrefixLookup<'_>],
    ) -> Option<std::sync::Arc<dyn KvPrefixIndexer>> {
        None
    }

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
