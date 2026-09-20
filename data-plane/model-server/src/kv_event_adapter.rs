// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Normalizes vLLM KV lifecycle events into privacy-preserving, rank-local delta streams.

use std::collections::{BTreeMap, HashMap, VecDeque};
use std::sync::{Arc, Mutex};

use foretoken_model_protocol::normalized_kv_block_hash;
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
    block: KvStoredBlock,
    placement: KvPlacement,
}

struct PendingStore {
    partition: KvPartition,
    placement: KvPlacement,
    group_idx: Option<u32>,
    parent_raw_hash: Option<Vec<u8>>,
    raw_blocks: Vec<(Vec<u8>, Vec<u32>)>,
    cache_salt: Option<String>,
}

struct RankState {
    epoch: String,
    ring: VecDeque<KvDelta>,
    raw_blocks: HashMap<(KvPlacement, Option<u32>, Vec<u8>), StoredBlock>,
    pending_stores: HashMap<(KvPlacement, Option<u32>, Vec<u8>), Vec<PendingStore>>,
    last_publisher_sequence: Option<u64>,
    available: bool,
}

impl RankState {
    fn new() -> Self {
        Self {
            epoch: Uuid::new_v4().to_string(),
            ring: VecDeque::new(),
            raw_blocks: HashMap::new(),
            pending_stores: HashMap::new(),
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
        self.pending_stores.clear();
        self.push(KvDeltaEvent::AllBlocksCleared);
    }

    // A lagging reader needs current facts, not deltas whose ancestors already left the ring.
    // Keep publisher continuity while rebasing the consumer stream onto its retained blocks.
    fn rebase_replay(&mut self) {
        self.epoch = Uuid::new_v4().to_string();
        self.ring.clear();
        let mut placements = BTreeMap::<KvPlacement, Vec<KvStoredBlock>>::new();
        for stored in self.raw_blocks.values() {
            placements
                .entry(stored.placement)
                .or_default()
                .push(stored.block.clone());
        }
        for (placement, mut blocks) in placements {
            blocks.sort_by(|a, b| {
                (&a.partition, a.block_index, &a.block_hash).cmp(&(
                    &b.partition,
                    b.block_index,
                    &b.block_hash,
                ))
            });
            self.push(KvDeltaEvent::BlockStored { blocks, placement });
        }
    }
}

struct Inner {
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
        let mut inner = self.inner.lock().unwrap();
        let Some(rank) = inner.ranks.get_mut(&dp_rank).filter(|rank| rank.available) else {
            return Err(KvDeltaError::Unavailable);
        };
        if epoch == Some(rank.epoch.as_str())
            && rank.ring.front().is_some_and(|delta| {
                after.map_or(0, |cursor| cursor.saturating_add(1)) < delta.sequence
            })
        {
            rank.rebase_replay();
        }
        let rank = &inner.ranks[&dp_rank];
        if epoch != Some(rank.epoch.as_str()) || after.is_some_and(|cursor| cursor > rank.current())
        {
            return Err(KvDeltaError::CursorReset(self.response(
                &inner,
                dp_rank,
                0,
                Vec::new(),
            )));
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
            epoch: inner.ranks[&dp_rank].epoch.clone(),
            dp_rank,
            through,
            current: inner.ranks.get(&dp_rank).map_or(0, RankState::current),
            deltas,
        }
    }

    fn fail_stream(&self, dp_rank: u32, reason: &'static str) {
        let mut inner = self.inner.lock().unwrap();
        if let Some(rank) = inner.ranks.get_mut(&dp_rank) {
            if rank.available {
                tracing::warn!(dp_rank, reason, "KV event adapter degraded");
            }
            // Recovery starts a new source epoch so readers cannot replay pre-gap facts.
            *rank = RankState::new();
            rank.available = false;
        }
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

    // Decode one raw vLLM event into the rank's normalized cache view. Invalid input clears
    // the source epoch; later valid batches rebuild only newly observed prefix chains.
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
            rank.pending_stores
                .retain(|(placement, group, parent), stores| {
                    let placement_matches =
                        event_placement.is_none_or(|expected| expected == *placement);
                    let group_matches =
                        event_group_idx.is_none_or(|expected| *group == Some(expected));
                    if placement_matches && group_matches && parent == &raw_hash {
                        return false;
                    }
                    stores.retain(|store| {
                        !(placement_matches
                            && group_matches
                            && store
                                .raw_blocks
                                .iter()
                                .any(|(candidate, _)| candidate == &raw_hash))
                    });
                    !stores.is_empty()
                });
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
            let group_idx = event_group_idx.or(stored.block.partition.group_idx);
            removed
                .entry((placement, group_idx))
                .or_default()
                .push(stored.block.block_hash);
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
        // Device events from older engines may omit plain-text extra keys. Offload events must
        // carry one entry per block: otherwise a salted store is indistinguishable from unsalted
        // content. Mooncake Store also reports its remote memory as CPU without these keys, so it
        // remains visible only through the connector's separate live shared-prefix query.
        let extra_keys = field(fields, "extra_keys");
        if field(fields, "lora_id").is_some_and(|value| !value.is_nil())
            || field(fields, "lora_name").is_some_and(|value| !value.is_nil())
        {
            return true;
        }
        // vLLM emits salt only in the root block. LoRA has separate event fields;
        // multimodal and embedding extra keys are not scalar-string root salts.
        let parent_raw_hash = field(fields, "parent_block_hash").and_then(raw_hash);
        let cache_salt = match extra_keys {
            None | Some(Value::Nil) if placement.tier == KvStorageTier::Device => None,
            None | Some(Value::Nil) => return true,
            Some(Value::Array(keys)) if keys.len() == raw_hashes.len() => {
                let mut salt = None;
                for (index, key) in keys.iter().enumerate() {
                    match key {
                        Value::Nil => {}
                        Value::Array(values)
                            if index == 0 && parent_raw_hash.is_none() && values.len() == 1 =>
                        {
                            let Some(value) = values[0].as_str() else {
                                return true;
                            };
                            salt = Some(value);
                        }
                        _ => return true,
                    }
                }
                salt
            }
            _ => return true,
        };
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
        let Some(raw_blocks) = raw_hashes
            .iter()
            .zip(token_ids.chunks_exact(block_size as usize))
            .map(|(raw, tokens)| Some((raw_hash(raw)?, tokens.to_vec())))
            .collect::<Option<Vec<_>>>()
        else {
            return false;
        };
        let pending = PendingStore {
            partition,
            placement,
            group_idx,
            parent_raw_hash,
            raw_blocks,
            cache_salt: cache_salt.map(str::to_owned),
        };
        let mut inner = self.inner.lock().unwrap();
        self.apply_pending_store(inner.ranks.get_mut(&dp_rank).unwrap(), pending);
        true
    }

    // OffloadingConnector may publish child chunks before their parents. Retain only the native
    // parent relation and resolve normalized identities once the complete prefix chain is known.
    fn apply_pending_store(&self, rank: &mut RankState, store: PendingStore) {
        let mut queue = VecDeque::from([store]);
        while let Some(store) = queue.pop_front() {
            let (mut parent_hash, first_block_index) = match store.parent_raw_hash.as_ref() {
                Some(raw_hash) => {
                    let key = (store.placement, store.group_idx, raw_hash.clone());
                    let Some(parent) = rank.raw_blocks.get(&key) else {
                        rank.pending_stores.entry(key).or_default().push(store);
                        continue;
                    };
                    (
                        parent.block.block_hash.clone(),
                        parent.block.block_index.saturating_add(1),
                    )
                }
                None => (KvBlockHash(String::new()), 0),
            };
            let mut blocks = Vec::with_capacity(store.raw_blocks.len());
            let mut resolved_raw_hashes = Vec::with_capacity(store.raw_blocks.len());
            for (offset, (raw_hash, tokens)) in store.raw_blocks.into_iter().enumerate() {
                let block_index = first_block_index.saturating_add(offset as u64);
                let block_hash = normalized_kv_block_hash(
                    &self.key,
                    &parent_hash,
                    &tokens,
                    &store.partition,
                    store.cache_salt.as_deref(),
                );
                let block = KvStoredBlock {
                    partition: store.partition.clone(),
                    block_index,
                    parent_hash: parent_hash.clone(),
                    block_hash: block_hash.clone(),
                };
                rank.raw_blocks.insert(
                    (store.placement, store.group_idx, raw_hash.clone()),
                    StoredBlock {
                        block: block.clone(),
                        placement: store.placement,
                    },
                );
                resolved_raw_hashes.push(raw_hash);
                blocks.push(block);
                parent_hash = block_hash;
            }
            if !blocks.is_empty() {
                rank.push(KvDeltaEvent::BlockStored {
                    blocks,
                    placement: store.placement,
                });
            }
            for raw_hash in resolved_raw_hashes {
                let key = (store.placement, store.group_idx, raw_hash);
                if let Some(children) = rank.pending_stores.remove(&key) {
                    queue.extend(children);
                }
            }
        }
    }

    /// Runs the owned ZMQ subscriber task started by model-server bootstrap.
    ///
    /// `ready` publishes listener readiness once. Bound sockets accept publisher reconnects;
    /// interrupted ranks remain unavailable until a valid batch starts their new epoch.
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
                            match event {
                                Some(zeromq::SocketEvent::Disconnected(_)) => {
                                    adapter.fail_stream(dp_rank, "event_stream_interrupted");
                                }
                                None | Some(zeromq::SocketEvent::Closed) => {
                                    adapter.fail_stream(dp_rank, "event_stream_interrupted");
                                    return;
                                }
                                _ => {}
                            }
                            continue;
                        }
                    };
                    match message {
                        Ok(message) => {
                            let frames = message
                                .into_vec()
                                .into_iter()
                                .map(|frame| frame.to_vec())
                                .collect();
                            adapter.ingest_frames(dp_rank, frames);
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
