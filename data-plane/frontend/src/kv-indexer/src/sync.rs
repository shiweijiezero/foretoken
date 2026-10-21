// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Event-source synchronization, route bindings, and index health.

mod observations;

use crate::*;
use foretoken_model_protocol::{
    KV_INDEX_DELTA_PATH, KvDeltaEvent, KvDeltaQuery, KvDeltaResponse, KvHashFormat, KvPlacement,
};
use futures::{StreamExt, stream};
use serde::{Deserialize, Serialize};
use std::{
    collections::{BTreeMap, BTreeSet},
    sync::Mutex,
    time::{Duration, Instant},
};

const OBSERVATION_CONCURRENCY: usize = 16;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvEventSourceConfig {
    pub event_source_id: String,
    pub model_group_id: String,
    pub endpoint: String,
    pub dp_rank: u32,
    pub model_revision: String,
    pub scope_id: String,
}
/// Maps routing candidates to cache sources and shared-storage capabilities.
/// Native local observations also report the engine's active offload tiers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvRouteBinding {
    pub data_parallel_rank_event_source_ids: BTreeMap<u32, String>,
    #[serde(default)]
    pub readable_placements: BTreeSet<KvPlacement>,
    #[serde(default)]
    pub can_restore_or_transfer: bool,
    /// Controller-owned query-sharing scope; layout compatibility is checked separately.
    #[serde(default)]
    pub shared_lookup_scope: Option<String>,
    /// Exact placement owned by that query provider.
    #[serde(default)]
    pub shared_lookup_placement: Option<KvPlacement>,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvRuntimeConfig {
    pub event_sources: Vec<KvEventSourceConfig>,
    pub route_bindings: BTreeMap<String, KvRouteBinding>,
    #[serde(default)]
    pub requested_implementation: KvLocalityIndexImplementation,
}

/// Configuration cannot leave a route's event source ambiguous.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum KvRuntimeConfigError {
    DuplicateEventSourceId(String),
    BindingSourceMissing {
        route_id: String,
        event_source_id: String,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KvIndexDegradedReason {
    KeyMissing,
    KeyReadFailed,
    KeyInvalidLength,
    SourceUnavailable,
    DeltaTransport,
    DeltaHttpStatus,
    EventSubscriberUnavailable,
    DeltaDecode,
    DeltaIdentityMismatch,
    DeltaCursorReset,
    DeltaSequenceGap,
    DeltaSequenceInvalid,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum KvIndexState {
    Disabled,
    Starting,
    Healthy,
    Degraded,
}
/// Observable state of the indexer and its source stream health.
#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct KvIndexStatus {
    /// The configured index implementation request.
    pub requested_implementation: KvLocalityIndexImplementation,
    /// The implementation selected for the observed topology.
    pub resolved_implementation: KvLocalityIndexResolvedImplementation,
    /// The real topology fact that selected Auto, absent for an explicit implementation.
    pub auto_resolution_reason: Option<KvAutoResolutionReason>,
    /// Aggregate state across all configured event sources.
    pub state: KvIndexState,
    /// Most recent failure reason while the indexer is degraded.
    pub reason: Option<KvIndexDegradedReason>,
    /// Number of sources that completed a valid refresh.
    pub sources_healthy: usize,
    /// Number of configured event sources.
    pub sources_total: usize,
}
struct State {
    key: [u8; 32],
    index: Mutex<KvLocalityIndexes>,
    cursors: Mutex<BTreeMap<String, Cursor>>,
    refresh: tokio::sync::Mutex<()>,
}
#[derive(Clone, Default)]
struct Cursor {
    epoch: String,
    after: Option<u64>,
    cache_groups: Option<Vec<foretoken_model_protocol::KvCacheGroup>>,
}
pub struct KvIndexer {
    config: KvRuntimeConfig,
    resolved: KvLocalityIndexResolvedImplementation,
    auto_resolution_reason: Option<KvAutoResolutionReason>,
    state: Option<State>,
    health: Mutex<(
        KvIndexState,
        Option<KvIndexDegradedReason>,
        BTreeMap<String, bool>,
    )>,
    client: reqwest::Client,
}
impl KvIndexer {
    /// Validates runtime bindings and creates the frontend-owned KV locality indexer.
    ///
    /// Frontend startup retains the indexer and calls `refresh`; a missing key yields a disabled, query-safe indexer.
    pub fn new(
        config: KvRuntimeConfig,
        key: Option<[u8; 32]>,
    ) -> Result<Self, KvRuntimeConfigError> {
        let mut source_ids = BTreeSet::new();
        for source in &config.event_sources {
            if !source_ids.insert(source.event_source_id.as_str()) {
                return Err(KvRuntimeConfigError::DuplicateEventSourceId(
                    source.event_source_id.clone(),
                ));
            }
        }
        for (route_id, binding) in &config.route_bindings {
            for event_source_id in binding.data_parallel_rank_event_source_ids.values() {
                if !source_ids.contains(event_source_id.as_str()) {
                    return Err(KvRuntimeConfigError::BindingSourceMissing {
                        route_id: route_id.clone(),
                        event_source_id: event_source_id.clone(),
                    });
                }
            }
        }

        let mut sources_by_scope = BTreeMap::<&str, BTreeSet<&str>>::new();
        for source in &config.event_sources {
            sources_by_scope
                .entry(&source.scope_id)
                .or_default()
                .insert(&source.event_source_id);
        }
        let topology = KvLocalityTopology {
            event_source_count: config.event_sources.len(),
            has_scope_with_distinct_sources: sources_by_scope
                .values()
                .any(|sources| sources.len() > 1),
            has_non_primary_data_parallel_rank: config.event_sources.iter().any(|s| s.dp_rank > 0),
            has_readable_tier_continuation: config.route_bindings.values().any(|binding| {
                binding.can_restore_or_transfer
                    && binding.readable_placements.iter().any(|placement| {
                        placement.tier != foretoken_model_protocol::KvStorageTier::Device
                    })
            }),
        };
        let auto_resolution_reason = (config.requested_implementation
            == KvLocalityIndexImplementation::Auto)
            .then(|| topology.resolution_reason());
        let resolved = config.requested_implementation.resolve(&topology);
        let state = key.map(|key| State {
            key,
            index: Mutex::new(KvLocalityIndexes::new(resolved, Duration::from_secs(300))),
            cursors: Mutex::new(BTreeMap::new()),
            refresh: tokio::sync::Mutex::new(()),
        });
        let status = if state.is_none() {
            KvIndexState::Disabled
        } else if config.event_sources.is_empty() {
            KvIndexState::Healthy
        } else {
            KvIndexState::Starting
        };
        let sources = config
            .event_sources
            .iter()
            .map(|s| (s.event_source_id.clone(), false))
            .collect();
        Ok(Self {
            config,
            resolved,
            auto_resolution_reason,
            state,
            health: Mutex::new((status, None, sources)),
            client: kv_http_client(),
        })
    }
    /// Creates an indexer that reports an unrecoverable initialization failure.
    ///
    /// Frontend startup retains this status-only fallback so routing can treat locality facts as unavailable.
    pub fn degraded(config: KvRuntimeConfig, reason: KvIndexDegradedReason) -> Self {
        let resolved = config
            .requested_implementation
            .resolve(&KvLocalityTopology::default());
        let sources = config
            .event_sources
            .iter()
            .map(|source| (source.event_source_id.clone(), false))
            .collect();
        Self {
            config,
            resolved,
            auto_resolution_reason: None,
            state: None,
            health: Mutex::new((KvIndexState::Degraded, Some(reason), sources)),
            client: kv_http_client(),
        }
    }

    /// Returns a point-in-time health and implementation report for telemetry consumers.
    ///
    /// The result is derived from indexer-owned state and does not grant access to cached locality facts.
    pub fn status(&self) -> KvIndexStatus {
        let h = self.health.lock().unwrap();
        KvIndexStatus {
            requested_implementation: self.config.requested_implementation,
            resolved_implementation: self.resolved,
            auto_resolution_reason: self.auto_resolution_reason,
            state: h.0,
            reason: h.1,
            sources_healthy: h.2.values().filter(|v| **v).count(),
            sources_total: h.2.len(),
        }
    }
    /// Refreshes every configured source and updates index-owned locality facts and health.
    ///
    /// The frontend refresh loop keeps the event-based fallback current independently of live queries.
    pub async fn refresh(&self) {
        let Some(state) = &self.state else { return };
        let _lock = state.refresh.lock().await;
        // Fetch independently with bounded concurrency, then apply serially while holding only the
        // short-lived index/cursor locks. A stuck producer therefore cannot block its peers.
        let updates = stream::iter(self.config.event_sources.iter().cloned().map(|source| {
            let cursor = state
                .cursors
                .lock()
                .unwrap()
                .get(&source.event_source_id)
                .cloned()
                .unwrap_or_default();
            async move {
                (
                    source.clone(),
                    fetch_delta(&self.client, source, cursor).await,
                )
            }
        }))
        .buffer_unordered(OBSERVATION_CONCURRENCY)
        .collect::<Vec<_>>()
        .await;
        for (source, result) in updates {
            match result {
                Ok(delta) => match apply(state, &source, delta) {
                    Ok(()) => self.ok(&source),
                    Err(reason) => self.fail(&source, reason),
                },
                Err(reason) => self.fail(&source, reason),
            }
        }
    }

    /// Resolves a routing identity and DP rank to the snapshot's exact cache source.
    fn route_source(
        &self,
        lookup: &KvPrefixLookup<'_>,
    ) -> Result<(&KvRouteBinding, &KvEventSourceConfig), KvPrefixUnavailableReason> {
        let binding = self
            .config
            .route_bindings
            .get(lookup.route_target_id)
            .ok_or(KvPrefixUnavailableReason::MissingBinding)?;
        let source_id = binding
            .data_parallel_rank_event_source_ids
            .get(&lookup.data_parallel_rank)
            .ok_or(KvPrefixUnavailableReason::RankMismatch)?;
        let source = self
            .config
            .event_sources
            .iter()
            .find(|source| &source.event_source_id == source_id)
            .ok_or(KvPrefixUnavailableReason::MissingBinding)?;
        Ok((binding, source))
    }

    /// Reads the synchronized event index when a live engine observation is unavailable.
    fn event_prefix_matches(&self, lookup: KvPrefixLookup<'_>) -> KvPrefixQueryResult {
        if lookup.lora_name.is_some() || !lookup.multimodal.is_empty() {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::UnsupportedRequest);
        }
        let (binding, source) = match self.route_source(&lookup) {
            Ok(route) => route,
            Err(reason) => return KvPrefixQueryResult::Unavailable(reason),
        };
        let Some(state) = &self.state else {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::Disabled);
        };
        if !self
            .health
            .lock()
            .unwrap()
            .2
            .get(&source.event_source_id)
            .copied()
            .unwrap_or(false)
        {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::SourceUnhealthy);
        }
        let cursor = state
            .cursors
            .lock()
            .unwrap()
            .get(&source.event_source_id)
            .cloned()
            .unwrap_or_default();
        if cursor.epoch.is_empty() {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::SourceUnhealthy);
        }
        // Only one dense group has complete-prefix semantics in the current event index.
        // Missing metadata and hybrid/sparse layouts cannot establish a reusable model prefix.
        let Some([group]) = cursor.cache_groups.as_deref() else {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::UnsupportedLayout);
        };
        if group.spec_kind != "full_attention" || group.sliding_window.is_some() {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::UnsupportedLayout);
        }
        let identity = KvEventSourceId {
            event_source_id: source.event_source_id.clone(),
            model_group_id: source.model_group_id.clone(),
            epoch: cursor.epoch,
            dp_rank: source.dp_rank,
        };
        let q = KvPrefixQuery {
            tokens: lookup.prompt_token_ids,
            cache_salt: lookup.cache_salt,
            model_revision: &source.model_revision,
            scope_id: &source.scope_id,
            hash_format: KvHashFormat::NormalizedKeyedBlake3V1,
            group_idx: Some(group.group_idx),
            match_all_groups: false,
            spec_kind: &group.spec_kind,
            sliding_window: group.sliding_window,
        };
        let mut index = state.index.lock().unwrap();
        let mut matches = index.query(&identity, &q, &state.key, Instant::now());
        let block_size = index.block_size(&identity, &q);
        drop(index);
        matches.retain(|m| {
            m.placement.locality != foretoken_model_protocol::KvCacheLocality::Unspecified
                && binding.readable_placements.contains(&m.placement)
                && (m.placement.tier == foretoken_model_protocol::KvStorageTier::Device
                    || binding.can_restore_or_transfer)
        });
        if let Some(block_size) = block_size {
            let block_size = block_size as usize;
            let reusable =
                lookup.prompt_token_ids.len().saturating_sub(1) / block_size * block_size;
            for matched in &mut matches {
                matched.matched_tokens = matched.matched_tokens.min(reusable);
            }
        }
        KvPrefixQueryResult::Matches(KvPrefixMatches::new(matches))
    }
    fn ok(&self, s: &KvEventSourceConfig) {
        let mut h = self.health.lock().unwrap();
        h.2.insert(s.event_source_id.clone(), true);
        if h.2.values().all(|x| *x) {
            h.0 = KvIndexState::Healthy;
            h.1 = None
        }
    }
    fn fail(&self, s: &KvEventSourceConfig, r: KvIndexDegradedReason) {
        if let Some(state) = &self.state {
            clear(state, s)
        }
        let mut h = self.health.lock().unwrap();
        h.2.insert(s.event_source_id.clone(), false);
        h.0 = KvIndexState::Degraded;
        h.1 = Some(r)
    }
}
impl std::fmt::Display for KvRuntimeConfigError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::DuplicateEventSourceId(id) => write!(f, "duplicate KV event source {id:?}"),
            Self::BindingSourceMissing {
                route_id,
                event_source_id,
            } => write!(
                f,
                "route {route_id:?} binds missing KV event source {event_source_id:?}"
            ),
        }
    }
}

impl std::error::Error for KvRuntimeConfigError {}

impl KvIndexState {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Disabled => "disabled",
            Self::Starting => "starting",
            Self::Healthy => "healthy",
            Self::Degraded => "degraded",
        }
    }
}

impl KvIndexDegradedReason {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::KeyMissing => "key_missing",
            Self::KeyReadFailed => "key_read_failed",
            Self::KeyInvalidLength => "key_invalid_length",
            Self::SourceUnavailable => "source_unavailable",
            Self::DeltaTransport => "delta_transport",
            Self::DeltaHttpStatus => "delta_http_status",
            Self::EventSubscriberUnavailable => "event_subscriber_unavailable",
            Self::DeltaDecode => "delta_decode",
            Self::DeltaIdentityMismatch => "delta_identity_mismatch",
            Self::DeltaCursorReset => "delta_cursor_reset",
            Self::DeltaSequenceGap => "delta_sequence_gap",
            Self::DeltaSequenceInvalid => "delta_sequence_invalid",
        }
    }
}

fn kv_http_client() -> reqwest::Client {
    // Bound connection, response, and body reads so a hung source cannot retain a refresh round.
    reqwest::Client::builder()
        .timeout(foretoken_model_protocol::KV_OBSERVATION_TIMEOUT)
        .build()
        .expect("static KV index HTTP client configuration is valid")
}

/// Fetches one source page for refresh without advancing its cursor or mutating the index.
/// HTTP 409 carries reset state; `apply` owns epoch validation and invalidation for both responses.
async fn fetch_delta(
    client: &reqwest::Client,
    source: KvEventSourceConfig,
    cursor: Cursor,
) -> Result<KvDeltaResponse, KvIndexDegradedReason> {
    let url = format!(
        "{}{}",
        source.endpoint.trim_end_matches('/'),
        KV_INDEX_DELTA_PATH
    );
    let query = KvDeltaQuery {
        dp_rank: source.dp_rank,
        epoch: (!cursor.epoch.is_empty()).then_some(cursor.epoch),
        after: cursor.after,
        limit: Some(256),
    };
    let response = client
        .get(url)
        .query(&query)
        .send()
        .await
        .map_err(|_| KvIndexDegradedReason::DeltaTransport)?;
    match response.status().as_u16() {
        200..=299 | 409 => response
            .json()
            .await
            .map_err(|_| KvIndexDegradedReason::DeltaDecode),
        503 => Err(KvIndexDegradedReason::EventSubscriberUnavailable),
        _ => Err(KvIndexDegradedReason::DeltaHttpStatus),
    }
}

/// Validates and applies one source delta before advancing its owned cursor.
/// Invalid identity or continuity clears only that source's cached locality facts.
fn apply(
    state: &State,
    s: &KvEventSourceConfig,
    d: KvDeltaResponse,
) -> Result<(), KvIndexDegradedReason> {
    if d.event_source_id != s.event_source_id
        || d.model_group_id != s.model_group_id
        || d.dp_rank != s.dp_rank
        || d.through > d.current
    {
        clear(state, s);
        return Err(KvIndexDegradedReason::DeltaIdentityMismatch);
    }
    let old = state
        .cursors
        .lock()
        .unwrap()
        .get(&s.event_source_id)
        .cloned()
        .unwrap_or_default();
    let epoch_reset = !old.epoch.is_empty() && old.epoch != d.epoch;
    if epoch_reset {
        // A new epoch restarts the source-local, zero-based sequence and invalidates old facts.
        clear(state, s);
    }
    let after = if old.epoch == d.epoch {
        old.after
    } else {
        None
    };
    if epoch_reset && d.deltas.first().is_some_and(|delta| delta.sequence != 0) {
        return Err(KvIndexDegradedReason::DeltaCursorReset);
    }
    if d.deltas.is_empty() {
        if d.through != after.unwrap_or(0) {
            clear(state, s);
            return Err(KvIndexDegradedReason::DeltaSequenceInvalid);
        }
    } else {
        let first_expected = after.map_or(0, |x| x + 1);
        for (offset, x) in d.deltas.iter().enumerate() {
            let expected = first_expected.saturating_add(offset as u64);
            if x.sequence != expected {
                clear(state, s);
                return Err(if x.sequence > expected {
                    KvIndexDegradedReason::DeltaSequenceGap
                } else {
                    KvIndexDegradedReason::DeltaSequenceInvalid
                });
            }
        }
        if d.deltas
            .last()
            .is_none_or(|delta| delta.sequence != d.through)
        {
            clear(state, s);
            return Err(KvIndexDegradedReason::DeltaSequenceInvalid);
        }
    }
    let id = KvEventSourceId {
        event_source_id: s.event_source_id.clone(),
        model_group_id: s.model_group_id.clone(),
        epoch: d.epoch.clone(),
        dp_rank: s.dp_rank,
    };
    let has = !d.deltas.is_empty();
    let mut index = state.index.lock().unwrap();
    for x in d.deltas {
        let e = match x.event {
            KvDeltaEvent::BlockStored { blocks, placement } => {
                KvIndexEvent::BlockStored { blocks, placement }
            }
            KvDeltaEvent::BlockRemoved {
                block_hashes,
                placement,
                group_idx,
            } => KvIndexEvent::BlockRemoved {
                block_hashes,
                placement,
                group_idx,
            },
            KvDeltaEvent::AllBlocksCleared => KvIndexEvent::AllBlocksCleared,
        };
        index.apply(id.clone(), e, Instant::now())
    }
    index.touch_source(&id, Instant::now());
    drop(index);
    state.cursors.lock().unwrap().insert(
        s.event_source_id.clone(),
        Cursor {
            epoch: d.epoch,
            after: has.then_some(d.through).or(after),
            cache_groups: d.cache_groups,
        },
    );
    Ok(())
}
/// Invalidates one source's cursor and facts together after refresh detects a broken epoch or page.
/// Other sources retain their independent observations and routing eligibility.
fn clear(state: &State, s: &KvEventSourceConfig) {
    if let Some(c) = state.cursors.lock().unwrap().remove(&s.event_source_id) {
        state.index.lock().unwrap().clear_source(&KvEventSourceId {
            event_source_id: s.event_source_id.clone(),
            model_group_id: s.model_group_id.clone(),
            epoch: c.epoch,
            dp_rank: s.dp_rank,
        })
    } else {
        state
            .index
            .lock()
            .unwrap()
            .clear_event_source(&s.event_source_id)
    }
}
