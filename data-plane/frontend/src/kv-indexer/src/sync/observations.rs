// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request-local engine and connector observations for cache-aware routing.

use super::{KvEventSourceConfig, KvIndexer, OBSERVATION_CONCURRENCY};
use crate::{
    KvPrefixIndexer, KvPrefixLookup, KvPrefixMatch, KvPrefixMatches, KvPrefixQueryResult,
    KvPrefixReuseScope, KvPrefixUnavailableReason,
};
use foretoken_model_protocol::{
    KV_LOCAL_PREFIX_PATH, KV_OBSERVATION_TIMEOUT, KV_RETAINED_PREFIX_PATH, KV_SHARED_PREFIX_PATH,
    KvCacheLocality, KvLocalPrefixResponse, KvMultimodalIdentity, KvPlacement, KvPrefixRequest,
    KvRetainedPrefixResponse, KvReuseSource, KvSharedPrefixResponse, KvStorageTier,
};
use futures::{StreamExt, stream};
use serde::de::DeserializeOwned;
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Arc,
};

/// Shared observations are interchangeable only within the same storage, layout and cache identity.
#[derive(Clone, PartialEq, Eq, PartialOrd, Ord)]
struct SharedPrefixKey {
    store_scope: String,
    model_scope: String,
    cache_salt: Option<String>,
    lora_name: Option<String>,
    lora_path: Option<String>,
    multimodal: Vec<KvMultimodalIdentity>,
    placement: KvPlacement,
}

/// One routing request owns this reader; live observations never enter the event index.
struct PreparedPrefixes(BTreeMap<(String, u32), KvPrefixQueryResult>);
impl KvPrefixIndexer for PreparedPrefixes {
    fn prefix_matches(&self, lookup: KvPrefixLookup<'_>) -> KvPrefixQueryResult {
        self.0
            .get(&(lookup.route_target_id.to_owned(), lookup.data_parallel_rank))
            .cloned()
            .unwrap_or(KvPrefixQueryResult::Unavailable(
                KvPrefixUnavailableReason::MissingBinding,
            ))
    }
}

#[async_trait::async_trait]
impl KvPrefixIndexer for KvIndexer {
    async fn prepare(
        &self,
        lookups: &[KvPrefixLookup<'_>],
        reuse_scopes: &[KvPrefixReuseScope<'_>],
    ) -> Option<Arc<dyn KvPrefixIndexer>> {
        self.state.as_ref()?;
        // Resolve candidates before I/O. One local query covers every DP scheduler;
        // shared queries are deduplicated by their storage, layout and request identity.
        let mut local_queries = BTreeMap::new();
        let mut shared_queries = BTreeMap::new();
        let mut shared_targets = BTreeMap::new();
        for lookup in lookups {
            let Ok((binding, source)) =
                self.route_source(lookup.route_target_id, lookup.data_parallel_rank)
            else {
                continue;
            };
            local_queries
                .entry(source.model_group_id.clone())
                .or_insert_with(|| (source.clone(), lookup.to_request()));
            let (Some(scope), Some(placement)) = (
                binding.shared_lookup_scope.as_deref(),
                binding.shared_lookup_placement,
            ) else {
                continue;
            };
            if !binding.can_restore_or_transfer || !binding.readable_placements.contains(&placement)
            {
                continue;
            }
            let key = SharedPrefixKey {
                store_scope: scope.to_owned(),
                model_scope: source.scope_id.clone(),
                cache_salt: lookup.cache_salt.map(str::to_owned),
                lora_name: lookup.lora_name.map(str::to_owned),
                lora_path: lookup.lora_path.map(str::to_owned),
                multimodal: lookup.multimodal.clone(),
                placement,
            };
            shared_queries
                .entry(key.clone())
                .or_insert_with(|| (source.clone(), lookup.to_request()));
            shared_targets.insert(
                (lookup.route_target_id.to_owned(), lookup.data_parallel_rank),
                key,
            );
        }
        if local_queries.is_empty() {
            return None;
        }

        // Transfer compatibility comes from Router; event scopes also include scheduler settings.
        let mut retained_queries = BTreeMap::new();
        let mut retained_targets = BTreeMap::<_, BTreeSet<_>>::new();
        for lookup in lookups {
            if lookup.lora_name.is_some()
                || lookup.lora_path.is_some()
                || !lookup.multimodal.is_empty()
            {
                continue;
            }
            let Ok((_, receiver)) =
                self.route_source(lookup.route_target_id, lookup.data_parallel_rank)
            else {
                continue;
            };
            for scope in reuse_scopes.iter().filter(|scope| {
                scope
                    .prefill_route_target_ids
                    .contains(&lookup.route_target_id)
            }) {
                for &source_id in &scope.decode_route_target_ids {
                    let Some((&data_parallel_rank, _)) = self
                        .config
                        .route_bindings
                        .get(source_id)
                        .and_then(|binding| {
                            binding
                                .data_parallel_rank_event_source_ids
                                .first_key_value()
                        })
                    else {
                        continue;
                    };
                    let Ok((binding, source)) = self.route_source(source_id, data_parallel_rank)
                    else {
                        continue;
                    };
                    if source.model_revision != receiver.model_revision {
                        continue;
                    }
                    retained_queries
                        .entry(source_id.to_owned())
                        .or_insert_with(|| {
                            let mut request = lookup.to_request();
                            request.dp_rank = data_parallel_rank;
                            (
                                source.clone(),
                                request,
                                binding
                                    .data_parallel_rank_event_source_ids
                                    .keys()
                                    .copied()
                                    .collect(),
                            )
                        });
                    retained_targets
                        .entry((lookup.route_target_id.to_owned(), lookup.data_parallel_rank))
                        .or_default()
                        .insert(source_id.to_owned());
                }
            }
        }

        // Run local, shared, and retained-prefix observations within one routing budget.
        let local_queries = stream::iter(local_queries.into_iter().map(
            |(id, (source, request))| async move {
                (
                    id,
                    fetch_local_prefix(&self.client, &source, &request).await,
                )
            },
        ))
        .buffer_unordered(OBSERVATION_CONCURRENCY);
        let shared_queries = stream::iter(shared_queries.into_iter().map(
            |(key, (source, request))| async move {
                (
                    key,
                    fetch_shared_prefix(&self.client, &source, &request).await,
                )
            },
        ))
        .buffer_unordered(OBSERVATION_CONCURRENCY);
        let retained_queries = stream::iter(retained_queries.into_iter().map(
            |(id, (source, request, ranks))| async move {
                (
                    id,
                    fetch_retained_prefix(&self.client, &source, &request, &ranks).await,
                )
            },
        ))
        .buffer_unordered(OBSERVATION_CONCURRENCY);
        let (local_observations, shared_observations, retained_observations) = tokio::join!(
            observe_prefixes(local_queries),
            observe_prefixes(shared_queries),
            observe_prefixes(retained_queries),
        );

        // Bind results back to exact candidates. A confirmed miss is usable information;
        // only an unavailable native result falls back to synchronized event facts.
        let mut prepared = BTreeMap::new();
        for lookup in lookups {
            let id = (lookup.route_target_id.to_owned(), lookup.data_parallel_rank);
            let rank = self
                .route_source(lookup.route_target_id, lookup.data_parallel_rank)
                .ok()
                .and_then(|(_, source)| local_observations.get(&source.model_group_id))
                .and_then(Option::as_ref)
                .and_then(|response| {
                    response
                        .ranks
                        .iter()
                        .find(|rank| rank.dp_rank == lookup.data_parallel_rank)
                });
            let shared = shared_targets.get(&id).and_then(|key| {
                shared_observations
                    .get(key)
                    .and_then(Option::as_ref)
                    .filter(|response| response.placement == key.placement)
            });
            let fallback = if rank.is_none() {
                Some(self.event_prefix_matches(lookup.clone()))
            } else {
                None
            };
            let known_local =
                rank.is_some() || matches!(fallback, Some(KvPrefixQueryResult::Matches(_)));
            let mut matches = match fallback {
                Some(KvPrefixQueryResult::Matches(matches)) => matches.into_iter().collect(),
                _ => Vec::new(),
            };
            if let Some(rank) = rank {
                matches.push(KvPrefixMatch {
                    placement: KvPlacement {
                        tier: KvStorageTier::Device,
                        locality: KvCacheLocality::Local,
                    },
                    matched_tokens: rank.matched_tokens,
                    kv_source: None,
                });
                // Each offload result already includes the locally reusable prefix.
                matches.extend(rank.offloaded.iter().map(|hit| KvPrefixMatch {
                    placement: KvPlacement {
                        tier: hit.tier,
                        locality: KvCacheLocality::Local,
                    },
                    matched_tokens: hit.matched_tokens,
                    kv_source: None,
                }));
            }
            if let Some(hit) = shared {
                matches.push(KvPrefixMatch {
                    placement: hit.placement,
                    matched_tokens: hit.matched_tokens,
                    kv_source: None,
                });
            }

            // Prefix lengths overlap across tiers; only a longer Decode prefix improves reuse.
            let existing_tokens = matches
                .iter()
                .map(|hit| hit.matched_tokens)
                .max()
                .unwrap_or(0);
            let retained = retained_targets
                .get(&id)
                .into_iter()
                .flatten()
                .filter_map(|source_id| {
                    retained_observations
                        .get(source_id)
                        .and_then(Option::as_ref)
                        .map(|response| (source_id, response))
                })
                .flat_map(|(source_id, response)| {
                    response.ranks.iter().filter_map(move |rank| {
                        let offer = rank.offer.as_ref()?;
                        (offer.matched_tokens > existing_tokens).then(|| KvReuseSource {
                            route_target_id: source_id.clone(),
                            dp_rank: rank.dp_rank,
                            instance_id: rank.instance_id.clone(),
                            model_server_endpoint: response.model_server_endpoint.clone(),
                            offer_id: offer.id.clone(),
                            matched_tokens: offer.matched_tokens,
                        })
                    })
                })
                .max_by_key(|source| source.matched_tokens);
            let has_retained = retained.is_some();
            if let Some(source) = retained {
                matches.push(KvPrefixMatch {
                    placement: KvPlacement {
                        tier: KvStorageTier::Device,
                        locality: KvCacheLocality::Remote,
                    },
                    matched_tokens: source.matched_tokens,
                    kv_source: Some(source),
                });
            }
            let result = if known_local || shared.is_some() || has_retained {
                KvPrefixQueryResult::Matches(KvPrefixMatches::new(matches))
            } else {
                KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::SourceUnhealthy)
            };
            prepared.insert(id, result);
        }
        Some(Arc::new(PreparedPrefixes(prepared)))
    }

    fn prefix_matches(&self, lookup: KvPrefixLookup<'_>) -> KvPrefixQueryResult {
        self.event_prefix_matches(lookup)
    }
}

/// Retains completed observations within the routing deadline, even if a peer stalls.
async fn observe_prefixes<K: Ord, V>(
    queries: impl futures::Stream<Item = (K, V)>,
) -> BTreeMap<K, V> {
    tokio::pin!(queries);
    let deadline = tokio::time::sleep(KV_OBSERVATION_TIMEOUT);
    tokio::pin!(deadline);
    let mut observations = BTreeMap::new();
    loop {
        tokio::select! {
            _ = &mut deadline => break,
            observation = queries.next() => match observation {
                Some((key, value)) => { observations.insert(key, value); }
                None => break,
            },
        }
    }
    observations
}

/// Reads one provider response; transport and decoding failures leave the observation unavailable.
async fn fetch_prefix<T: DeserializeOwned>(
    client: &reqwest::Client,
    source: &KvEventSourceConfig,
    path: &str,
    request: &KvPrefixRequest,
) -> Option<T> {
    let response = client
        .post(format!("{}{}", source.endpoint.trim_end_matches('/'), path))
        .json(request)
        .send()
        .await
        .ok()?;
    if !response.status().is_success() {
        return None;
    }
    response.json().await.ok()
}

/// Accepts local engine observations only for the requested model and unambiguous DP ranks.
async fn fetch_local_prefix(
    client: &reqwest::Client,
    source: &KvEventSourceConfig,
    request: &KvPrefixRequest,
) -> Option<KvLocalPrefixResponse> {
    let response: KvLocalPrefixResponse =
        fetch_prefix(client, source, KV_LOCAL_PREFIX_PATH, request).await?;
    let mut ranks = BTreeSet::new();
    let maximum = request.prompt_token_ids.len().saturating_sub(1);
    if response.model_group_id != source.model_group_id
        || response.scope_id != source.scope_id
        || response.ranks.iter().any(|rank| {
            !ranks.insert(rank.dp_rank)
                || rank.matched_tokens > maximum
                || rank.offloaded.iter().any(|hit| {
                    hit.matched_tokens > maximum
                        || !matches!(hit.tier, KvStorageTier::HostPinned | KvStorageTier::Disk)
                })
        })
    {
        return None;
    }
    Some(response)
}

/// Validates a retained-prefix response's endpoint URL and configured source bindings.
async fn fetch_retained_prefix(
    client: &reqwest::Client,
    source: &KvEventSourceConfig,
    request: &KvPrefixRequest,
    allowed_ranks: &BTreeSet<u32>,
) -> Option<KvRetainedPrefixResponse> {
    let mut response: KvRetainedPrefixResponse =
        fetch_prefix(client, source, KV_RETAINED_PREFIX_PATH, request).await?;
    let endpoint = reqwest::Url::parse(&response.model_server_endpoint).ok()?;
    let mut ranks = BTreeSet::new();
    if !matches!(endpoint.scheme(), "http" | "https")
        || endpoint.host_str().is_none()
        || response.model_group_id != source.model_group_id
        || response.scope_id != source.scope_id
        || response.ranks.iter().any(|rank| {
            !ranks.insert(rank.dp_rank)
                || !allowed_ranks.contains(&rank.dp_rank)
                || rank.instance_id.is_empty()
                || rank.offer.as_ref().is_some_and(|offer| {
                    offer.id.is_empty()
                        || offer.matched_tokens == 0
                        || offer.matched_tokens > request.prompt_token_ids.len()
                })
        })
    {
        return None;
    }
    response.ranks.sort_by_key(|rank| rank.dp_rank);
    Some(response)
}

/// Accepts connector observations only for the requested model and a known cache placement.
async fn fetch_shared_prefix(
    client: &reqwest::Client,
    source: &KvEventSourceConfig,
    request: &KvPrefixRequest,
) -> Option<KvSharedPrefixResponse> {
    let response: KvSharedPrefixResponse =
        fetch_prefix(client, source, KV_SHARED_PREFIX_PATH, request).await?;
    if response.model_group_id != source.model_group_id
        || response.scope_id != source.scope_id
        || response.placement.locality == KvCacheLocality::Unspecified
        || response.matched_tokens > request.prompt_token_ids.len().saturating_sub(1)
    {
        return None;
    }
    Some(response)
}
