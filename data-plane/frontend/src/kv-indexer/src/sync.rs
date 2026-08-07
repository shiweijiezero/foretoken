// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//! HTTP synchronization and router prefix-score integration.

use std::collections::BTreeMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use foretoken_model_protocol::{KvDeltaEvent, KvDeltaResponse};
use foretoken_router::{BackendId, KvPrefixScorer, RouteContext};
use serde::Serialize;

use crate::index::{Delta, KvIndex, Source, Stored};

/// One model-server endpoint that publishes KV index deltas for a backend and scope.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KvEventSourceConfig {
    pub backend_id: BackendId,
    pub endpoint: String,
    pub scope_id: String,
}

/// Binds a public route to the source that owns its reusable prefill KV state.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KvRouteBinding {
    pub source_backend_id: BackendId,
}

/// Validated, immutable KV runtime inputs derived from a routing snapshot.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct KvRuntimeConfig {
    pub event_sources: Vec<KvEventSourceConfig>,
    pub route_bindings: BTreeMap<BackendId, KvRouteBinding>,
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
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct KvIndexStatus {
    pub state: KvIndexState,
    pub reason: Option<KvIndexDegradedReason>,
    pub sources_healthy: usize,
    pub sources_total: usize,
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
        }
    }
}

#[derive(Debug, Clone)]
struct KvIndexHealth {
    state: KvIndexState,
    reason: Option<KvIndexDegradedReason>,
    sources: BTreeMap<BackendId, bool>,
}

/// Synchronizes opaque model-server KV events. Failures remove only the untrusted hint while
/// remaining explicit through status, metrics, and transition logs.
pub struct KvIndexer {
    config: KvRuntimeConfig,
    state: Option<KvIndexerState>,
    health: Mutex<KvIndexHealth>,
    client: reqwest::Client,
}

struct KvIndexerState {
    key: [u8; 32],
    index: Mutex<KvIndex>,
    cursors: Mutex<BTreeMap<BackendId, KvIndexCursor>>,
    refresh: tokio::sync::Mutex<()>,
}

#[derive(Clone, Default)]
struct KvIndexCursor {
    epoch: String,
    after: u64,
}

impl KvIndexer {
    /// A missing digest key explicitly disables indexing without affecting routing eligibility.
    pub fn new(config: KvRuntimeConfig, key: Option<[u8; 32]>) -> Self {
        let state_kind = if key.is_none() {
            KvIndexState::Disabled
        } else if config.event_sources.is_empty() {
            KvIndexState::Healthy
        } else {
            KvIndexState::Starting
        };
        Self::build(config, key, state_kind, None)
    }

    /// Records a configured credential failure while preserving serving availability.
    pub fn degraded(config: KvRuntimeConfig, reason: KvIndexDegradedReason) -> Self {
        Self::build(config, None, KvIndexState::Degraded, Some(reason))
    }

    fn build(
        config: KvRuntimeConfig,
        key: Option<[u8; 32]>,
        state_kind: KvIndexState,
        reason: Option<KvIndexDegradedReason>,
    ) -> Self {
        let sources = config
            .event_sources
            .iter()
            .map(|source| (source.backend_id.clone(), false))
            .collect();
        Self {
            config,
            state: key.map(|key| KvIndexerState {
                key,
                index: Mutex::new(KvIndex::new(Duration::from_secs(300))),
                cursors: Mutex::new(BTreeMap::new()),
                refresh: tokio::sync::Mutex::new(()),
            }),
            health: Mutex::new(KvIndexHealth {
                state: state_kind,
                reason,
                sources,
            }),
            client: reqwest::Client::builder()
                .timeout(Duration::from_secs(1))
                .build()
                .expect("static KV client configuration must be valid"),
        }
    }

    pub fn status(&self) -> KvIndexStatus {
        let health = self.health.lock().unwrap();
        KvIndexStatus {
            state: health.state,
            reason: health.reason,
            sources_healthy: health.sources.values().filter(|healthy| **healthy).count(),
            sources_total: health.sources.len(),
        }
    }

    /// Fetches bounded delta pages for every source. Failures discard only affected soft state.
    pub async fn refresh(&self) {
        let Some(state) = &self.state else {
            return;
        };
        let _refresh = state.refresh.lock().await;
        for source in &self.config.event_sources {
            if !ready(&self.client, &source.endpoint).await {
                clear_component(state, &source.backend_id);
                self.mark_source(
                    &source.backend_id,
                    false,
                    KvIndexDegradedReason::SourceUnavailable,
                );
                continue;
            }
            // `through`, rather than `current`, makes every bounded page lossless.
            for _ in 0..4 {
                let cursor = state
                    .cursors
                    .lock()
                    .unwrap()
                    .get(&source.backend_id)
                    .cloned()
                    .unwrap_or_default();
                let url = format!(
                    "{}/v1/internal/kv-index/delta?epoch={}&after={}&limit=256",
                    source.endpoint.trim_end_matches('/'),
                    cursor.epoch,
                    cursor.after
                );
                match self.client.get(url).send().await {
                    Ok(response) if response.status().is_success() => {
                        match response.json::<KvDeltaResponse>().await {
                            Ok(delta) => match apply_component(state, source, delta) {
                                Ok(()) => self.mark_source_healthy(&source.backend_id),
                                Err(reason) => {
                                    self.mark_source(&source.backend_id, false, reason);
                                    break;
                                }
                            },
                            Err(_) => {
                                clear_component(state, &source.backend_id);
                                self.mark_source(
                                    &source.backend_id,
                                    false,
                                    KvIndexDegradedReason::DeltaDecode,
                                );
                                break;
                            }
                        }
                    }
                    Ok(response) if response.status().as_u16() == 409 => {
                        clear_component(state, &source.backend_id);
                        self.mark_source(
                            &source.backend_id,
                            false,
                            KvIndexDegradedReason::DeltaCursorReset,
                        );
                        if let Ok(delta) = response.json::<KvDeltaResponse>().await
                            && delta.backend_id == source.backend_id.as_str()
                            && delta.scope_id == source.scope_id
                            && delta.dp_rank == 0
                        {
                            state.cursors.lock().unwrap().insert(
                                source.backend_id.clone(),
                                KvIndexCursor {
                                    epoch: delta.epoch,
                                    after: 0,
                                },
                            );
                        }
                        break;
                    }
                    Ok(response) if response.status().as_u16() == 503 => {
                        clear_component(state, &source.backend_id);
                        self.mark_source(
                            &source.backend_id,
                            false,
                            KvIndexDegradedReason::EventSubscriberUnavailable,
                        );
                        break;
                    }
                    Ok(_) => {
                        clear_component(state, &source.backend_id);
                        self.mark_source(
                            &source.backend_id,
                            false,
                            KvIndexDegradedReason::DeltaHttpStatus,
                        );
                        break;
                    }
                    Err(_) => {
                        clear_component(state, &source.backend_id);
                        self.mark_source(
                            &source.backend_id,
                            false,
                            KvIndexDegradedReason::DeltaTransport,
                        );
                        break;
                    }
                }
                let next = state
                    .cursors
                    .lock()
                    .unwrap()
                    .get(&source.backend_id)
                    .cloned()
                    .unwrap_or_default();
                if next.after == cursor.after {
                    break;
                }
            }
        }
    }

    fn mark_source_healthy(&self, backend_id: &BackendId) {
        let mut health = self.health.lock().unwrap();
        health.sources.insert(backend_id.clone(), true);
        if health.sources.values().all(|healthy| *healthy) {
            if health.state != KvIndexState::Healthy {
                tracing::info!("KV index recovered");
            }
            health.state = KvIndexState::Healthy;
            health.reason = None;
        }
    }

    fn mark_source(&self, backend_id: &BackendId, healthy: bool, reason: KvIndexDegradedReason) {
        let mut health = self.health.lock().unwrap();
        health.sources.insert(backend_id.clone(), healthy);
        if health.state != KvIndexState::Degraded || health.reason != Some(reason) {
            tracing::warn!(backend_id = %backend_id.as_str(), ?reason, "KV index degraded; routing continues without the affected locality hint");
        }
        health.state = KvIndexState::Degraded;
        health.reason = Some(reason);
    }

    fn route_score(&self, backend_id: &BackendId, context: RouteContext<'_>) -> usize {
        let request = context.request;
        if request.prompt_token_ids.is_empty()
            || request.cache_salt.is_some()
            || request.lora_request.is_some()
            || request.mm_features.is_some()
            || request.sampling_params.skip_reading_prefix_cache == Some(true)
            || request.data_parallel_rank.is_some_and(|rank| rank != 0)
        {
            // Current opaque KV deltas cannot reconstruct request-specific cache partitions.
            return 0;
        }
        let Some(state) = &self.state else {
            return 0;
        };
        let Some(binding) = self.config.route_bindings.get(backend_id) else {
            return 0;
        };
        let Some(source) = self
            .config
            .event_sources
            .iter()
            .find(|source| source.backend_id == binding.source_backend_id)
        else {
            return 0;
        };
        if source.scope_id.is_empty() {
            return 0;
        }
        state.index.lock().unwrap().score_backend(
            source.backend_id.as_str(),
            &source.scope_id,
            &request.prompt_token_ids,
            &state.key,
            Instant::now(),
        )
    }
}

impl KvPrefixScorer for KvIndexer {
    fn score_prefill_prefix(&self, backend_id: &BackendId, context: RouteContext<'_>) -> usize {
        self.route_score(backend_id, context)
    }
}

fn apply_component(
    state: &KvIndexerState,
    source: &KvEventSourceConfig,
    delta: KvDeltaResponse,
) -> Result<(), KvIndexDegradedReason> {
    if delta.backend_id != source.backend_id.as_str()
        || delta.scope_id != source.scope_id
        || delta.dp_rank != 0
        || delta.through > delta.current
    {
        clear_component(state, &source.backend_id);
        return Err(KvIndexDegradedReason::DeltaIdentityMismatch);
    }
    let old = state
        .cursors
        .lock()
        .unwrap()
        .get(&source.backend_id)
        .cloned()
        .unwrap_or_default();
    if !old.epoch.is_empty() && old.epoch != delta.epoch {
        clear_component(state, &source.backend_id);
        return Err(KvIndexDegradedReason::DeltaCursorReset);
    }
    let index_source = Source {
        backend_id: source.backend_id.as_str().into(),
        epoch: delta.epoch.clone(),
        dp_rank: 0,
    };
    {
        let mut index = state.index.lock().unwrap();
        for delta_event in delta.deltas {
            let _sequence = delta_event.sequence;
            let delta = match delta_event.event {
                KvDeltaEvent::Store { partition, digest } => {
                    Delta::Store(Stored { partition, digest })
                }
                KvDeltaEvent::Remove { partition, digest } => {
                    Delta::Remove(Stored { partition, digest })
                }
                KvDeltaEvent::Clear => Delta::Clear,
            };
            index.apply(index_source.clone(), delta, Instant::now());
        }
        index.touch_backend(source.backend_id.as_str(), Instant::now());
    }
    state.cursors.lock().unwrap().insert(
        source.backend_id.clone(),
        KvIndexCursor {
            epoch: delta.epoch,
            after: delta.through,
        },
    );
    Ok(())
}

fn clear_component(state: &KvIndexerState, backend_id: &BackendId) {
    state
        .index
        .lock()
        .unwrap()
        .clear_backend(backend_id.as_str());
    state.cursors.lock().unwrap().remove(backend_id);
}

async fn ready(client: &reqwest::Client, endpoint: &str) -> bool {
    let url = format!("{}/readyz", endpoint.trim_end_matches('/'));
    matches!(client.get(url).send().await, Ok(response) if response.status().is_success())
}

#[cfg(test)]
#[path = "tests/index.rs"]
mod tests;
