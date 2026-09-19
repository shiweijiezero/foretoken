// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Normalizes vLLM KV lifecycle events into privacy-preserving, rank-local delta streams.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::{Arc, Mutex};

use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use foretoken_model_protocol::{
    KvBlockHash, KvCacheLocality, KvDelta, KvDeltaEvent, KvDeltaResponse, KvHashFormat,
    KvPartition, KvPlacement, KvStorageTier, KvStoredBlock,
};
use rmpv::Value;
use uuid::Uuid;
use zeromq::SubSocket;
use zeromq::prelude::{Socket, SocketRecv};

use crate::runtime_transport::{KV_EVENT_TOPIC, kv_event_endpoint};

const CAPACITY: usize = 4096;

#[derive(Clone)]
struct StoredBlock {
    partition: KvPartition,
    block_index: u64,
    block_hash: KvBlockHash,
    placement: KvPlacement,
}

struct RankState {
    ring: VecDeque<KvDelta>,
    raw_blocks: HashMap<(KvPlacement, Option<u32>, Vec<u8>), StoredBlock>,
    last_publisher_sequence: Option<u64>,
    available: bool,
}

impl RankState {
    fn new() -> Self {
        Self {
            ring: VecDeque::new(),
            raw_blocks: HashMap::new(),
            last_publisher_sequence: None,
            available: true,
        }
    }

    fn current(&self) -> u64 {
        self.ring.back().map_or(0, |delta| delta.sequence)
    }

    fn push(&mut self, event: KvDeltaEvent) {
        let sequence = self
            .ring
            .back()
            .map_or(0, |delta| delta.sequence.saturating_add(1));
        self.ring.push_back(KvDelta { sequence, event });
        if self.ring.len() > CAPACITY {
            self.ring.pop_front();
        }
    }

    fn clear(&mut self) {
        self.raw_blocks.clear();
        self.push(KvDeltaEvent::AllBlocksCleared);
    }
}

struct Inner {
    epoch: String,
    ranks: BTreeMap<u32, RankState>,
}

#[derive(Debug)]
pub enum KvDeltaError {
    Unavailable,
    CursorReset(KvDeltaResponse),
}

pub struct KvEventAdapter {
    inner: Mutex<Inner>,
    key: [u8; 32],
    scope_id: String,
    model_group_id: String,
    model_revision: String,
    data_parallel_size: u32,
}

impl KvEventAdapter {
    /// Creates a model-server-local adapter with independent KV event state for every data-parallel rank.
    pub fn new(
        key: [u8; 32],
        scope_id: String,
        model_group_id: String,
        model_revision: String,
        data_parallel_size: u32,
    ) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(Inner {
                epoch: Uuid::new_v4().to_string(),
                ranks: (0..data_parallel_size)
                    .map(|rank| (rank, RankState::new()))
                    .collect(),
            }),
            key,
            scope_id,
            model_group_id,
            model_revision,
            data_parallel_size,
        })
    }

    /// Returns bounded rank-local deltas for the internal KV index endpoint.
    ///
    /// The adapter retains cursor state; this response owns its cloned events or signals a reset.
    pub fn delta(
        &self,
        dp_rank: u32,
        epoch: Option<&str>,
        after: Option<u64>,
        limit: usize,
    ) -> Result<KvDeltaResponse, KvDeltaError> {
        let inner = self.inner.lock().unwrap();
        let Some(rank) = inner.ranks.get(&dp_rank).filter(|rank| rank.available) else {
            return Err(KvDeltaError::Unavailable);
        };
        let reset = self.response(&inner, dp_rank, 0, Vec::new());
        if epoch != Some(inner.epoch.as_str())
            || after.is_some_and(|cursor| cursor > rank.current())
            || after.is_some_and(|cursor| {
                rank.ring
                    .front()
                    .is_some_and(|delta| cursor.saturating_add(1) < delta.sequence)
            })
        {
            return Err(KvDeltaError::CursorReset(reset));
        }
        let deltas = rank
            .ring
            .iter()
            .filter(|delta| after.is_none_or(|cursor| delta.sequence > cursor))
            .take(limit.min(512))
            .cloned()
            .collect::<Vec<_>>();
        let through = deltas
            .last()
            .map(|delta| delta.sequence)
            .or(after)
            .unwrap_or(0);
        Ok(self.response(&inner, dp_rank, through, deltas))
    }

    fn response(
        &self,
        inner: &Inner,
        dp_rank: u32,
        through: u64,
        deltas: Vec<KvDelta>,
    ) -> KvDeltaResponse {
        KvDeltaResponse {
            event_source_id: format!("{}:dp:{dp_rank}", self.model_group_id),
            model_group_id: self.model_group_id.clone(),
            epoch: inner.epoch.clone(),
            dp_rank,
            through,
            current: inner.ranks.get(&dp_rank).map_or(0, RankState::current),
            deltas,
        }
    }

    fn fail_stream(&self, dp_rank: u32, reason: &'static str) {
        let mut inner = self.inner.lock().unwrap();
        if let Some(rank) = inner.ranks.get_mut(&dp_rank) {
            rank.clear();
            if rank.available {
                tracing::warn!(dp_rank, reason, "KV event adapter degraded");
            }
            rank.last_publisher_sequence = None;
            rank.available = false;
        }
    }

    fn normalized_hash(
        &self,
        parent: &KvBlockHash,
        tokens: &[u32],
        partition: &KvPartition,
    ) -> KvBlockHash {
        let mut hasher = blake3::Hasher::new_keyed(&self.key);
        hasher.update(parent.0.as_bytes());
        hasher.update(partition.model_revision.as_bytes());
        hasher.update(partition.scope_id.as_bytes());
        hasher.update(&partition.hash_block_size.to_le_bytes());
        hasher.update(&partition.group_idx.unwrap_or(u32::MAX).to_le_bytes());
        hasher.update(partition.spec_kind.as_bytes());
        hasher.update(&partition.sliding_window.unwrap_or(u32::MAX).to_le_bytes());
        for token in tokens {
            hasher.update(&token.to_le_bytes());
        }
        KvBlockHash(URL_SAFE_NO_PAD.encode(hasher.finalize().as_bytes()))
    }

    /// Establish the first observed sequence as a safe empty-state baseline, then require contiguity.
    fn ingest_frames(&self, dp_rank: u32, frames: Vec<Vec<u8>>) -> bool {
        if frames.len() != 3 || frames[0] != KV_EVENT_TOPIC.as_bytes() || frames[1].len() != 8 {
            self.fail_stream(dp_rank, "event_protocol_violation");
            return false;
        }
        let sequence = u64::from_be_bytes(frames[1].as_slice().try_into().unwrap());
        let previous = self.inner.lock().unwrap().ranks[&dp_rank].last_publisher_sequence;
        if previous.is_some_and(|current| current.checked_add(1) != Some(sequence)) {
            self.fail_stream(dp_rank, "event_sequence_gap");
            return false;
        }
        self.ingest_msgpack(dp_rank, &frames[2]);
        let mut inner = self.inner.lock().unwrap();
        let rank = inner.ranks.get_mut(&dp_rank).unwrap();
        if !rank.available {
            return false;
        }
        rank.last_publisher_sequence = Some(sequence);
        true
    }

    /// Ingests one vLLM msgspec event payload from the subscriber task into adapter-owned state.
    ///
    /// It publishes normalized deltas to later HTTP readers and retains no borrowed payload bytes.
    pub fn ingest_msgpack(&self, dp_rank: u32, bytes: &[u8]) {
        if dp_rank >= self.data_parallel_size {
            return;
        }
        let Ok(Value::Array(batch)) = rmp_serde::from_slice::<Value>(bytes) else {
            self.fail_stream(dp_rank, "event_protocol_violation");
            return;
        };
        let Some(events) = batch.get(1).and_then(Value::as_array) else {
            self.fail_stream(dp_rank, "event_protocol_violation");
            return;
        };
        if !(batch.len() == 2 || batch.len() == 3)
            || !matches!(batch[0], Value::Integer(_) | Value::F32(_) | Value::F64(_))
        {
            self.fail_stream(dp_rank, "event_protocol_violation");
            return;
        }
        let matching_rank = match batch.get(2) {
            Some(value) => value.as_u64() == Some(u64::from(dp_rank)),
            None => self.data_parallel_size == 1 && dp_rank == 0,
        };
        if !matching_rank {
            self.fail_stream(dp_rank, "event_rank_mismatch");
            return;
        }
        for event in events.iter().cloned() {
            if !self.ingest_event(dp_rank, event) {
                self.fail_stream(dp_rank, "event_protocol_violation");
                return;
            }
        }
        let mut inner = self.inner.lock().unwrap();
        let rank = inner.ranks.get_mut(&dp_rank).unwrap();
        if !rank.available {
            tracing::info!(dp_rank, "KV event adapter recovered");
        }
        rank.available = true;
    }

    // Decode one raw vLLM event into the rank's normalized cache view. Invalid input returns false
    // so the frame stage can clear retained state and end the subscriber lifecycle consistently.
    fn ingest_event(&self, dp_rank: u32, event: Value) -> bool {
        let Value::Map(fields) = event else {
            return false;
        };
        let event_type = field(&fields, "type")
            .or_else(|| field(&fields, "event_type"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if event_type.contains("AllBlocksCleared") {
            let mut inner = self.inner.lock().unwrap();
            inner.ranks.get_mut(&dp_rank).unwrap().clear();
            return true;
        }
        let Some(raw_hashes) = field(&fields, "block_hashes").and_then(Value::as_array) else {
            return false;
        };
        if event_type.contains("BlockRemoved") {
            return self.remove_blocks(dp_rank, raw_hashes, &fields);
        }
        if !event_type.contains("BlockStored") {
            return false;
        }
        self.store_blocks(dp_rank, raw_hashes, &fields)
    }

    // Remove matching raw entries and publish grouped normalized removals. Ambiguous hashes are
    // intentionally left untouched because the publisher did not identify their cache group.
    fn remove_blocks(&self, dp_rank: u32, raw_hashes: &[Value], fields: &[(Value, Value)]) -> bool {
        let event_placement = placement(field(fields, "medium"), field(fields, "locality"));
        let event_group_idx = optional_u32(field(fields, "group_idx"));
        let mut inner = self.inner.lock().unwrap();
        let rank = inner.ranks.get_mut(&dp_rank).unwrap();
        let mut removed = BTreeMap::<(KvPlacement, Option<u32>), Vec<KvBlockHash>>::new();
        for value in raw_hashes {
            let Some(raw_hash) = raw_hash(value) else {
                return false;
            };
            let keys = rank
                .raw_blocks
                .keys()
                .filter(|(placement, group, candidate)| {
                    candidate == &raw_hash
                        && event_placement.is_none_or(|expected| expected == *placement)
                        && event_group_idx.is_none_or(|expected| *group == Some(expected))
                })
                .cloned()
                .collect::<Vec<_>>();
            if keys.len() != 1 {
                continue;
            }
            let stored = rank.raw_blocks.remove(&keys[0]);
            let Some(stored) = stored else {
                continue;
            };
            let placement = event_placement.unwrap_or(stored.placement);
            let group_idx = event_group_idx.or(stored.partition.group_idx);
            removed
                .entry((placement, group_idx))
                .or_default()
                .push(stored.block_hash);
        }
        for ((placement, group_idx), block_hashes) in removed {
            rank.push(KvDeltaEvent::BlockRemoved {
                block_hashes,
                placement,
                group_idx,
            });
        }
        true
    }

    // Convert contiguous token blocks into keyed public identities before retaining their raw lookup
    // keys. The ring publishes only normalized blocks, while raw hashes remain adapter-private.
    fn store_blocks(&self, dp_rank: u32, raw_hashes: &[Value], fields: &[(Value, Value)]) -> bool {
        let Some(placement) = placement(field(fields, "medium"), field(fields, "locality")) else {
            return true;
        };
        // Plain-text events may carry one null extra-key entry per cached block.
        // Actual salt, multimodal or LoRA identity is outside this token-only index.
        let extra_keys = field(fields, "extra_keys");
        if extra_keys.is_some_and(|value| {
            !value.is_nil()
                && !value
                    .as_array()
                    .is_some_and(|keys| keys.iter().all(Value::is_nil))
        }) || field(fields, "lora_id").is_some_and(|value| !value.is_nil())
            || field(fields, "lora_name").is_some_and(|value| !value.is_nil())
        {
            return true;
        }
        let group_idx = optional_u32(field(fields, "group_idx"));
        let spec_kind = field(fields, "kv_cache_spec_kind")
            .and_then(Value::as_str)
            .unwrap_or("full_attention");
        let sliding_window = optional_u32(
            field(fields, "kv_cache_spec_sliding_window")
                .or_else(|| field(fields, "sliding_window")),
        );
        let (Some(token_values), Some(block_size)) = (
            field(fields, "token_ids").and_then(Value::as_array),
            field(fields, "block_size").and_then(Value::as_u64),
        ) else {
            return false;
        };
        let Ok(block_size) = u32::try_from(block_size) else {
            return false;
        };
        if block_size == 0
            || token_values.len() % block_size as usize != 0
            || raw_hashes.len() != token_values.len() / block_size as usize
        {
            return false;
        }
        let Some(token_ids) = token_values
            .iter()
            .map(|value| value.as_u64().and_then(|token| u32::try_from(token).ok()))
            .collect::<Option<Vec<_>>>()
        else {
            return false;
        };
        let partition = KvPartition {
            model_revision: self.model_revision.clone(),
            scope_id: self.scope_id.clone(),
            hash_format: KvHashFormat::NormalizedKeyedBlake3V1,
            hash_block_size: block_size,
            group_idx,
            spec_kind: spec_kind.into(),
            sliding_window,
        };
        let parent_raw_hash = field(fields, "parent_block_hash").and_then(raw_hash);
        let mut inner = self.inner.lock().unwrap();
        let rank = inner.ranks.get_mut(&dp_rank).unwrap();
        let (mut parent_hash, first_block_index) = match parent_raw_hash {
            Some(raw_hash) => match rank.raw_blocks.get(&(placement, group_idx, raw_hash)) {
                Some(parent) => (
                    parent.block_hash.clone(),
                    parent.block_index.saturating_add(1),
                ),
                None => return true,
            },
            None => (KvBlockHash(String::new()), 0),
        };
        let mut blocks = Vec::with_capacity(raw_hashes.len());
        for (offset, (raw_value, tokens)) in raw_hashes
            .iter()
            .zip(token_ids.chunks_exact(block_size as usize))
            .enumerate()
        {
            let Some(raw_hash) = raw_hash(raw_value) else {
                return false;
            };
            let block_index = first_block_index.saturating_add(offset as u64);
            let block_hash = self.normalized_hash(&parent_hash, tokens, &partition);
            let block = KvStoredBlock {
                partition: partition.clone(),
                block_index,
                parent_hash: parent_hash.clone(),
                block_hash: block_hash.clone(),
            };
            rank.raw_blocks.insert(
                (placement, group_idx, raw_hash),
                StoredBlock {
                    partition: partition.clone(),
                    block_index,
                    block_hash: block_hash.clone(),
                    placement,
                },
            );
            blocks.push(block);
            parent_hash = block_hash;
        }
        if !blocks.is_empty() {
            rank.push(KvDeltaEvent::BlockStored { blocks, placement });
        }
        true
    }

    /// Runs the owned ZMQ subscriber task started by model-server bootstrap.
    ///
    /// `ready` publishes connection status once; this task retains its adapter until stream failure.
    pub async fn serve(self: Arc<Self>, host: String, ready: tokio::sync::oneshot::Sender<bool>) {
        use futures::StreamExt;
        let mut streams = futures::stream::FuturesUnordered::new();
        for dp_rank in 0..self.data_parallel_size {
            let mut socket = SubSocket::new();
            let mut monitor = socket.monitor();
            if socket
                .bind(&kv_event_endpoint(&host, dp_rank))
                .await
                .is_err()
                || socket.subscribe(KV_EVENT_TOPIC).await.is_err()
            {
                self.fail_stream(dp_rank, "event_stream_interrupted");
                continue;
            }
            let adapter = self.clone();
            streams.push(async move {
                loop {
                    let message = tokio::select! {
                        message = socket.recv() => message,
                        event = monitor.next() => {
                            if matches!(event, None | Some(zeromq::SocketEvent::Disconnected(_) | zeromq::SocketEvent::Closed)) {
                                adapter.fail_stream(dp_rank, "event_stream_interrupted");
                                return;
                            }
                            continue;
                        }
                    };
                    match message {
                        Ok(message) => {
                            let frames = message.into_vec().into_iter().map(|frame| frame.to_vec()).collect();
                            if !adapter.ingest_frames(dp_rank, frames) {
                                return;
                            }
                        }
                        Err(_) => {
                            adapter.fail_stream(dp_rank, "event_stream_interrupted");
                            return;
                        }
                    }
                }
            });
        }
        let _ = ready.send(!streams.is_empty());
        while streams.next().await.is_some() {}
    }
}

fn field<'a>(fields: &'a [(Value, Value)], name: &str) -> Option<&'a Value> {
    fields
        .iter()
        .find_map(|(key, value)| (key.as_str() == Some(name)).then_some(value))
}

fn raw_hash(value: &Value) -> Option<Vec<u8>> {
    match value {
        Value::Binary(bytes) => Some(bytes.clone()),
        Value::Integer(integer) => integer.as_u64().map(|value| value.to_be_bytes().to_vec()),
        _ => None,
    }
}

fn optional_u32(value: Option<&Value>) -> Option<u32> {
    value
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
}

fn placement(medium: Option<&Value>, locality: Option<&Value>) -> Option<KvPlacement> {
    let medium = medium.and_then(Value::as_str).unwrap_or("GPU");
    let tier = match medium.to_ascii_uppercase().as_str() {
        "GPU" | "DEVICE" => KvStorageTier::Device,
        "CPU" | "CPU_PINNED" => KvStorageTier::HostPinned,
        "STORAGE" | "DISK" | "NVME" => KvStorageTier::Disk,
        "REMOTE" | "EXTERNAL" | "NETWORK" | "SHARED" => KvStorageTier::External,
        _ => return None,
    };
    let locality = match locality
        .and_then(Value::as_str)
        .map(str::to_ascii_uppercase)
    {
        Some(value) if matches!(value.as_str(), "LOCAL" | "GPU" | "CPU" | "CPU_PINNED") => {
            KvCacheLocality::Local
        }
        Some(value)
            if matches!(
                value.as_str(),
                "REMOTE" | "STORAGE" | "DISK" | "NVME" | "EXTERNAL" | "NETWORK" | "SHARED"
            ) =>
        {
            KvCacheLocality::Remote
        }
        Some(_) => KvCacheLocality::Unspecified,
        None if tier == KvStorageTier::External => KvCacheLocality::Remote,
        None => KvCacheLocality::Local,
    };
    Some(KvPlacement { tier, locality })
}
