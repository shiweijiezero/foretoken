// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Source-stream synchronization and route-bound typed locality queries.
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
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvEventSourceConfig {
    pub event_source_id: String,
    pub model_group_id: String,
    pub endpoint: String,
    pub dp_rank: u32,
    pub model_revision: String,
    pub scope_id: String,
    pub spec_kind: String,
    pub sliding_window: Option<u32>,
    pub group_idx: Option<u32>,
}
/// Binding separates router identity from owner identity and only permits lower-tier hints when a
/// candidate declares it can read, restore, or transfer the exact placement.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvRouteBinding {
    pub data_parallel_rank_event_source_ids: BTreeMap<u32, String>,
    #[serde(default)]
    pub readable_placements: BTreeSet<KvPlacement>,
    #[serde(default)]
    pub can_restore_or_transfer: bool,
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
    fn source(&self, id: &str) -> Option<&KvEventSourceConfig> {
        self.config
            .event_sources
            .iter()
            .find(|s| s.event_source_id == id)
    }
    fn query(&self, lookup: KvPrefixLookup<'_>) -> KvPrefixQueryResult {
        let Some(binding) = self.config.route_bindings.get(lookup.route_target_id) else {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::MissingBinding);
        };
        let Some(event_source_id) = binding
            .data_parallel_rank_event_source_ids
            .get(&lookup.data_parallel_rank)
        else {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::RankMismatch);
        };
        let Some(source) = self.source(event_source_id) else {
            return KvPrefixQueryResult::Unavailable(KvPrefixUnavailableReason::MissingBinding);
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
        let identity = KvEventSourceId {
            event_source_id: source.event_source_id.clone(),
            model_group_id: source.model_group_id.clone(),
            epoch: cursor.epoch,
            dp_rank: source.dp_rank,
        };
        let q = KvPrefixQuery {
            tokens: lookup.prompt_token_ids,
            model_revision: &source.model_revision,
            scope_id: &source.scope_id,
            hash_format: KvHashFormat::NormalizedKeyedBlake3V1,
            group_idx: source.group_idx,
            spec_kind: &source.spec_kind,
            sliding_window: source.sliding_window,
        };
        let mut matches =
            state
                .index
                .lock()
                .unwrap()
                .query(&identity, &q, &state.key, Instant::now());
        matches.retain(|m| {
            m.placement.locality != foretoken_model_protocol::KvCacheLocality::Unspecified
                && binding.readable_placements.contains(&m.placement)
                && (m.placement.tier == foretoken_model_protocol::KvStorageTier::Device
                    || binding.can_restore_or_transfer)
        });
        KvPrefixQueryResult::Matches(KvPrefixMatches::new(matches))
    }
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
        .buffer_unordered(16)
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
fn kv_http_client() -> reqwest::Client {
    // Bound connection, response, and body reads so a hung source cannot retain a refresh round.
    reqwest::Client::builder()
        .timeout(Duration::from_secs(5))
        .build()
        .expect("static KV index HTTP client configuration is valid")
}

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

impl KvPrefixIndexer for KvIndexer {
    fn prefix_matches(&self, lookup: KvPrefixLookup<'_>) -> KvPrefixQueryResult {
        self.query(lookup)
    }
}
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
        },
    );
    Ok(())
}
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
