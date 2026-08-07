// SPDX-License-Identifier: Apache-2.0
//! Best-effort, privacy-preserving vLLM KV event adapter. A bad event loses credit.
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use foretoken_model_protocol::{KvDelta, KvDeltaEvent, KvDeltaResponse, KvPartition};
use rmpv::Value;
use std::collections::{HashMap, VecDeque};
use std::sync::{Arc, Mutex};
use uuid::Uuid;
use zeromq::SubSocket;
use zeromq::prelude::{Socket, SocketRecv};
const TOPIC: &[u8] = b"foretoken-kv-v1";
const CAP: usize = 4096;
struct Inner {
    epoch: String,
    next: u64,
    ring: VecDeque<KvDelta>,
    raw: HashMap<Vec<u8>, (KvPartition, [u8; 32])>,
    expected: Option<u64>,
    available: bool,
}
#[derive(Debug)]
pub enum KvDeltaError {
    Unavailable,
    CursorReset(KvDeltaResponse),
}

pub struct KvEventAdapter {
    inner: Mutex<Inner>,
    key: [u8; 32],
    scope: String,
    backend_id: String,
}
impl KvEventAdapter {
    pub fn new(key: [u8; 32], scope: String, backend_id: String) -> Arc<Self> {
        Arc::new(Self {
            inner: Mutex::new(Inner {
                epoch: Uuid::new_v4().to_string(),
                next: 0,
                ring: VecDeque::new(),
                raw: HashMap::new(),
                expected: None,
                available: true,
            }),
            key,
            scope,
            backend_id,
        })
    }
    pub fn unavailable(&self) {
        self.mark_unavailable("event_stream_interrupted");
    }

    fn mark_unavailable(&self, reason: &'static str) {
        let mut inner = self.inner.lock().unwrap();
        if inner.available {
            tracing::warn!(reason, "KV event adapter degraded");
        }
        inner.available = false;
    }
    pub fn delta(
        &self,
        epoch: &str,
        after: u64,
        limit: usize,
    ) -> Result<KvDeltaResponse, KvDeltaError> {
        let i = self.inner.lock().unwrap();
        let clear = KvDeltaResponse {
            backend_id: self.backend_id.clone(),
            scope_id: self.scope.clone(),
            epoch: i.epoch.clone(),
            dp_rank: 0,
            through: 0,
            current: i.next,
            deltas: vec![],
        };
        if !i.available {
            return Err(KvDeltaError::Unavailable);
        }
        if epoch != i.epoch
            || after > i.next
            || (after != 0 && i.ring.front().is_some_and(|d| after + 1 < d.sequence))
        {
            return Err(KvDeltaError::CursorReset(clear));
        }
        let deltas = i
            .ring
            .iter()
            .filter(|d| d.sequence > after)
            .take(limit.min(512))
            .cloned()
            .collect::<Vec<_>>();
        let through = deltas.last().map(|d| d.sequence).unwrap_or(after);
        Ok(KvDeltaResponse {
            backend_id: self.backend_id.clone(),
            scope_id: self.scope.clone(),
            epoch: i.epoch.clone(),
            dp_rank: 0,
            through,
            current: i.next,
            deltas,
        })
    }
    fn push(i: &mut Inner, event: KvDeltaEvent) {
        i.next += 1;
        i.ring.push_back(KvDelta {
            sequence: i.next,
            event,
        });
        if i.ring.len() > CAP {
            i.ring.pop_front();
        }
    }
    pub fn clear(&self) {
        let mut i = self.inner.lock().unwrap();
        i.raw.clear();
        Self::push(&mut i, KvDeltaEvent::Clear)
    }
    fn gap(&self) {
        self.clear();
        self.inner.lock().unwrap().expected = None;
        self.mark_unavailable("event_sequence_gap");
    }

    fn protocol_failure(&self) {
        self.clear();
        self.mark_unavailable("event_protocol_violation");
    }
    fn digest(&self, parent: &[u8; 32], tokens: &[u32], p: &KvPartition) -> [u8; 32] {
        let mut h = blake3::Hasher::new_keyed(&self.key);
        h.update(parent);
        h.update(p.scope_id.as_bytes());
        h.update(&p.block_size.to_le_bytes());
        h.update(&p.group_idx.unwrap_or(u32::MAX).to_le_bytes());
        h.update(p.spec_kind.as_bytes());
        for t in tokens {
            h.update(&t.to_le_bytes());
        }
        *h.finalize().as_bytes()
    }
    /// Exactly topic, 8-byte BE sequence, MessagePack payload; gaps fail closed.
    pub fn ingest_frames(&self, f: Vec<Vec<u8>>) {
        if f.len() != 3 || f[0] != TOPIC || f[1].len() != 8 {
            self.gap();
            return;
        }
        let n = u64::from_be_bytes(f[1].as_slice().try_into().unwrap());
        {
            let mut i = self.inner.lock().unwrap();
            if i.expected.is_some_and(|x| x != n) {
                drop(i);
                self.gap();
                return;
            }
            i.expected = Some(n.wrapping_add(1));
        }
        self.ingest_msgpack(&f[2])
    }
    /// Pinned msgspec array-like payload: [timestamp, tagged events, optional DP rank].
    pub fn ingest_msgpack(&self, b: &[u8]) {
        let Ok(Value::Array(a)) = rmp_serde::from_slice::<Value>(b) else {
            self.protocol_failure();
            return;
        };
        if !(a.len() == 2 || a.len() == 3)
            || !matches!(a[0], Value::Integer(_) | Value::F32(_) | Value::F64(_))
            || a[1].as_array().is_none()
            || a.get(2).is_some_and(|x| x.as_i64() != Some(0))
        {
            self.protocol_failure();
            return;
        }
        for e in a[1].as_array().unwrap().iter().cloned() {
            if !self.event(e) {
                self.protocol_failure();
                return;
            }
        }
        let mut inner = self.inner.lock().unwrap();
        if !inner.available {
            tracing::info!("KV event adapter recovered");
        }
        inner.available = true;
    }
    fn event(&self, e: Value) -> bool {
        let Value::Map(m) = e else { return false };
        let f = |n: &str| {
            m.iter()
                .find_map(|(k, v)| (k.as_str() == Some(n)).then_some(v))
        };
        let ty = f("type")
            .or_else(|| f("event_type"))
            .and_then(Value::as_str)
            .unwrap_or("");
        if ty.contains("AllBlocksCleared") {
            self.clear();
            return true;
        }
        let hashes = f("block_hashes").and_then(Value::as_array);
        if ty.contains("BlockRemoved") {
            let Some(h) = hashes else { return false };
            let mut i = self.inner.lock().unwrap();
            for x in h {
                if let Some(k) = hash(x)
                    && let Some((p, d)) = i.raw.remove(&k)
                {
                    Self::push(
                        &mut i,
                        KvDeltaEvent::Remove {
                            partition: p,
                            digest: URL_SAFE_NO_PAD.encode(d),
                        },
                    )
                }
            }
            return true;
        }
        if !ty.contains("BlockStored") {
            return false;
        }
        let medium = f("medium").and_then(Value::as_str).unwrap_or("GPU");
        let loc = f("locality").and_then(Value::as_str);
        let extra = f("extra_keys");
        let group = f("group_idx").and_then(Value::as_i64);
        let spec = f("kv_cache_spec_kind")
            .and_then(Value::as_str)
            .unwrap_or("full_attention");
        // GPU events currently provide the complete store/remove/reset lifecycle required for an
        // authoritative prefix index. Offload and shared-store events remain untrusted hints until
        // they expose stable store identity and deletion semantics or a store-native lookup verifies them.
        if medium != "GPU"
            || matches!(loc, Some("CPU" | "STORAGE" | "REMOTE"))
            || extra.is_some_and(|x| !x.is_nil())
            || group.is_some_and(|x| x != 0)
            || spec != "full_attention"
        {
            return true;
        }
        let (Some(h), Some(t), Some(bs)) = (
            hashes,
            f("token_ids").and_then(Value::as_array),
            f("block_size").and_then(Value::as_u64),
        ) else {
            return false;
        };
        if bs == 0 || t.len() % bs as usize != 0 || h.len() != t.len() / bs as usize {
            return false;
        }
        let Some(ids) = t
            .iter()
            .map(|x| x.as_u64().map(|n| n as u32))
            .collect::<Option<Vec<_>>>()
        else {
            return false;
        };
        let p = KvPartition {
            scope_id: self.scope.clone(),
            block_size: bs as u32,
            group_idx: group.map(|x| x as u32),
            spec_kind: spec.into(),
        };
        let parent = f("parent_block_hash").and_then(hash);
        let mut i = self.inner.lock().unwrap();
        let mut prev = match parent {
            Some(k) => match i.raw.get(&k) {
                Some((_, d)) => *d,
                None => return true,
            },
            None => [0; 32],
        };
        for (x, c) in h.iter().zip(ids.chunks(bs as usize)) {
            let Some(k) = hash(x) else { return false };
            let d = self.digest(&prev, c, &p);
            i.raw.insert(k, (p.clone(), d));
            Self::push(
                &mut i,
                KvDeltaEvent::Store {
                    partition: p.clone(),
                    digest: URL_SAFE_NO_PAD.encode(d),
                },
            );
            prev = d
        }
        true
    }
    pub async fn serve(self: Arc<Self>) {
        self.serve_ready(None).await;
    }
    pub async fn serve_ready(self: Arc<Self>, ready: Option<tokio::sync::oneshot::Sender<bool>>) {
        let mut s = SubSocket::new();
        if s.bind("tcp://127.0.0.1:5557").await.is_err()
            || s.subscribe("foretoken-kv-v1").await.is_err()
        {
            self.unavailable();
            if let Some(ready) = ready {
                let _ = ready.send(false);
            }
            return;
        }
        if let Some(ready) = ready {
            let _ = ready.send(true);
        }
        loop {
            match s.recv().await {
                Ok(m) => self.ingest_frames(m.into_vec().into_iter().map(|x| x.to_vec()).collect()),
                Err(_) => {
                    self.gap();
                    self.unavailable();
                    return;
                }
            }
        }
    }
}
fn hash(v: &Value) -> Option<Vec<u8>> {
    match v {
        Value::Binary(x) => Some(x.clone()),
        Value::Integer(n) => n.as_u64().map(|x| x.to_be_bytes().to_vec()),
        _ => None,
    }
}
#[cfg(test)]
#[path = "tests/kv_event_adapter.rs"]
mod tests;
