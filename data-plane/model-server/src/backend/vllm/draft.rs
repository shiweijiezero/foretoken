// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns Draft peer sessions and their native EngineCore connection through shutdown.

mod http;
mod requests;

use std::collections::{BTreeMap, HashMap};
use std::future::Future;
use std::path::Path;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use async_trait::async_trait;
use reqwest::StatusCode;
use serde::{Deserialize, Serialize, de::DeserializeOwned};
use serde_json::{Value, json};
use tracing::warn;
use vllm_engine_core_client::EngineCoreClient;

use crate::execution::RuntimeHealth;
use crate::profiling::{EngineProfiler, ExportResult};
use requests::{OpenPeer, Peer};

const PEER_STATUS_PATH: &str = "/v1/internal/draft/status";

/// Retains the native connection and peer sessions for the model-server supervisor.
#[derive(Clone)]
pub struct DraftClient(Arc<DraftState>);

struct DraftState {
    engine: EngineCoreClient,
    requires_target_features: bool,
    health: Arc<RuntimeHealth>,
    stopping: AtomicBool,
    peers: Mutex<HashMap<String, Arc<Peer>>>,
}

/// Occupancy covers both parked native sessions and executing peer operations.
#[derive(Deserialize, Serialize)]
pub struct DraftStatus {
    pub accepting: bool,
    pub requires_target_features: bool,
    pub active_sessions: u64,
    pub in_flight_batches: u64,
}

/// A cancelled peer operation must not abandon native work or recycle its registered buffers.
struct OperationCancellation {
    client: DraftClient,
    armed: bool,
}

impl Drop for OperationCancellation {
    fn drop(&mut self) {
        if self.armed {
            self.client
                .fail("Draft operation cancelled before native completion".into());
        }
    }
}

/// The control connection owns its peer from native admission through explicit release.
struct PeerConnection {
    client: DraftClient,
    peer: Arc<Peer>,
}

impl Drop for PeerConnection {
    fn drop(&mut self) {
        if !self.peer.closed.is_cancelled() {
            self.client
                .fail("Target control connection closed before peer release".into());
        }
    }
}

impl DraftClient {
    /// Connects peer ingress to an initialized native Draft worker.
    pub async fn new(engine: EngineCoreClient) -> Result<Self, String> {
        let requirements: Vec<Vec<bool>> = engine
            .call_utility(
                "collective_rpc",
                (
                    "dt_requires_target_features",
                    None::<f64>,
                    Vec::<Value>::new(),
                    BTreeMap::<String, String>::new(),
                ),
            )
            .await
            .map_err(|error| error.to_string())?;
        let requires_target_features = requirements
            .into_iter()
            .flatten()
            .next()
            .ok_or("Native Draft worker did not return its feature requirements")?;
        Ok(Self(Arc::new(DraftState {
            engine,
            requires_target_features,
            health: Arc::new(RuntimeHealth::new()),
            stopping: AtomicBool::new(false),
            peers: Mutex::new(HashMap::new()),
        })))
    }

    /// Shares admission and liveness between the supervisor, peer sessions and HTTP handlers.
    pub fn health(&self) -> Arc<RuntimeHealth> {
        self.0.health.clone()
    }

    fn engine(&self) -> &EngineCoreClient {
        &self.0.engine
    }

    fn accepting(&self) -> bool {
        self.0.health.accepting()
    }

    fn healthy(&self) -> bool {
        self.engine().is_healthy() && self.0.health.healthy()
    }

    fn fail(&self, reason: String) {
        if self.0.stopping.load(Ordering::Acquire) || !self.0.health.healthy() {
            return;
        }
        warn!(%reason, "stopping Draft service");
        self.0.health.fail_execution();
    }

    /// Wakes the supervisor on native transport failure or a lost peer's unbounded buffer ownership.
    pub async fn wait_failed(&self) -> String {
        let mut health = self.engine().subscribe_health();
        loop {
            if !*health.borrow_and_update() {
                return self.engine().health_error().map_or_else(
                    || "Native Draft transport stopped".into(),
                    |error| error.to_string(),
                );
            }
            if !self.0.health.healthy() {
                return "Draft execution failed".into();
            }
            tokio::select! {
                () = self.0.health.execution_failed() => return "Draft execution failed".into(),
                result = health.changed() => if result.is_err() { return "Native Draft health observation closed".into(); },
            }
        }
    }

    /// Marks planned teardown before the supervisor stops native compute and tensor transport.
    pub fn begin_shutdown(&self) {
        self.0.health.set_accepting(false);
        self.0.stopping.store(true, Ordering::Release);
    }

    /// Reads retained occupancy for readiness and controller-owned draining.
    pub fn status(&self) -> Result<DraftStatus, String> {
        if !self.healthy() {
            return Err("Draft native execution is unavailable".into());
        }
        let peers = self.0.peers.lock().unwrap();
        Ok(DraftStatus {
            accepting: self.accepting(),
            requires_target_features: self.0.requires_target_features,
            active_sessions: peers
                .values()
                .map(|peer| peer.active_sessions() as u64)
                .sum(),
            in_flight_batches: peers.values().filter(|peer| peer.in_flight()).count() as u64,
        })
    }

    /// Closes new peers and requests while accepted sessions continue their native turns.
    pub fn drain(&self) -> Result<DraftStatus, String> {
        self.0.health.set_accepting(false);
        self.status()
    }

    /// Reads the selected Draft's native requirements before constructing Target engine configuration.
    pub async fn wait_peer(endpoint: &str) -> Result<DraftStatus, reqwest::Error> {
        let client = reqwest::Client::builder().no_proxy().build()?;
        let url = format!("{}{PEER_STATUS_PATH}", endpoint.trim_end_matches('/'));
        loop {
            let status = async {
                client
                    .get(&url)
                    .timeout(Duration::from_secs(1))
                    .send()
                    .await?
                    .error_for_status()?
                    .json::<DraftStatus>()
                    .await
            }
            .await;
            match status {
                Ok(status) if status.accepting => return Ok(status),
                Ok(_) => {}
                Err(error)
                    if error.is_connect()
                        || error.is_timeout()
                        || error.status() == Some(StatusCode::SERVICE_UNAVAILABLE) => {}
                Err(error) => return Err(error),
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    }

    /// Returns the Draft worker's response through native collective RPC.
    async fn rpc<T, A>(&self, method: &str, args: A) -> Result<T, String>
    where
        T: DeserializeOwned,
        A: Serialize + std::fmt::Debug,
    {
        let results: Vec<Vec<T>> = self
            .engine()
            .call_utility(
                "collective_rpc",
                (method, None::<f64>, args, BTreeMap::<String, String>::new()),
            )
            .await
            .map_err(|error| error.to_string())?;
        results
            .into_iter()
            .flatten()
            .next()
            .ok_or_else(|| format!("Native Draft worker did not return {method}"))
    }

    /// Keeps admission, transfer and cleanup owned after a peer stops waiting.
    async fn join_operation<T>(
        &self,
        future: impl Future<Output = Result<T, String>> + Send + 'static,
    ) -> Result<T, String>
    where
        T: Send + 'static,
    {
        let mut cancellation = OperationCancellation {
            client: self.clone(),
            armed: true,
        };
        let result = tokio::spawn(future)
            .await
            .map_err(|error| error.to_string());
        cancellation.armed = false;
        if let Err(error) = &result {
            self.fail(error.clone());
        }
        result?
    }

    /// Serializes one peer's operations while allowing native scheduling to batch all peers.
    async fn operate<T, F, Fut>(&self, peer: Arc<Peer>, work: F) -> Result<T, String>
    where
        T: Send + 'static,
        F: FnOnce(DraftClient, Arc<Peer>) -> Fut + Send + 'static,
        Fut: Future<Output = Result<T, String>> + Send + 'static,
    {
        if !self.healthy() {
            return Err("Draft native execution is unavailable".into());
        }
        let permit = peer
            .operation
            .clone()
            .try_acquire_owned()
            .map_err(|_| "DT peer is already executing a batch")?;
        if peer.closing.load(Ordering::Acquire) {
            return Err("DT peer is closing".into());
        }
        let client = self.clone();
        self.join_operation(async move {
            let _permit = permit;
            let result = work(client.clone(), peer).await;
            if let Err(error) = &result {
                client.fail(error.to_string());
            }
            result
        })
        .await
    }

    /// Registers native peer buffers and returns their control-connection owner.
    async fn open(&self, spec: OpenPeer) -> Result<(PeerConnection, Value), String> {
        if !self.healthy() {
            return Err("Draft native execution is unavailable".into());
        }
        if spec.max_model_len > self.engine().max_model_len() as usize {
            return Err("Target context limit exceeds the Draft context limit".into());
        }
        let peer = Arc::new(Peer::new(spec));
        let permit = peer
            .operation
            .clone()
            .try_acquire_owned()
            .expect("new peer is idle");
        {
            let mut peers = self.0.peers.lock().unwrap();
            if !self.accepting() {
                return Err("Draft is draining".into());
            }
            if peers.contains_key(&peer.spec.peer_id) {
                return Err("DT peer already exists".into());
            }
            peers.insert(peer.spec.peer_id.clone(), peer.clone());
        }
        let client = self.clone();
        let opened = peer.clone();
        let input = self
            .join_operation(async move {
                let _permit = permit;
                client
                    .rpc::<Value, _>("dt_open_peer", (&opened.spec,))
                    .await
            })
            .await;
        match input {
            Ok(input) => Ok((
                PeerConnection {
                    client: self.clone(),
                    peer,
                },
                json!({"event": "opened", "input": input}),
            )),
            Err(error) => {
                self.0.peers.lock().unwrap().remove(&peer.spec.peer_id);
                peer.closed.cancel();
                Err(error)
            }
        }
    }

    /// Joins native sessions before unregistering the peer's transport buffers.
    async fn close_peer(&self, peer: Arc<Peer>) -> Result<(), String> {
        if peer.closing.swap(true, Ordering::AcqRel) {
            return Err("DT peer is already closing".into());
        }
        let client = self.clone();
        self.join_operation(async move {
            let _permit = peer
                .operation
                .clone()
                .acquire_owned()
                .await
                .map_err(|error| error.to_string())?;
            let result: Result<(), String> = async {
                peer.finish(&client, None).await?;
                if !client.healthy() {
                    return Err("Draft transport is stopping".into());
                }
                client
                    .rpc::<(), _>("dt_close_peer", (&peer.spec.peer_id,))
                    .await?;
                client.0.peers.lock().unwrap().remove(&peer.spec.peer_id);
                peer.closed.cancel();
                Ok(())
            }
            .await;
            if let Err(error) = &result {
                client.fail(error.to_string());
            }
            result
        })
        .await
    }
}

#[async_trait]
impl EngineProfiler for DraftClient {
    async fn set_pytorch_recording(&self, start: bool, prefix: &str) -> Result<(), String> {
        super::profiling::set_pytorch_recording(self.engine(), start, prefix).await
    }

    async fn finish_pytorch_export(&self, prefix: &str) -> Result<ExportResult, String> {
        super::profiling::finish_pytorch_export(self.engine(), prefix).await
    }

    async fn set_mctracer_recording(&self, start: bool, directory: &Path) -> Result<(), String> {
        self.rpc("foretoken_mctracer", (start, directory.to_string_lossy()))
            .await
    }
}

impl DraftStatus {
    /// Reports whether retained requests and outstanding proposal or cleanup work have completed.
    pub fn drained(&self) -> bool {
        self.active_sessions == 0 && self.in_flight_batches == 0
    }
}
