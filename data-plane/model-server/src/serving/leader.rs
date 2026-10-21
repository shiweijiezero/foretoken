// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns leader ingress, execution health, and the common serving-to-drain transition.

use std::future::IntoFuture;
use std::io;
use std::sync::Arc;
use std::time::Instant;

use foretoken_model_protocol::{PreparedTokenizer, RuntimeMetadataResponse, RuntimeModelIdentity};
use foretoken_model_server::api::{AppState, router};
use foretoken_model_server::backend::VllmBackend;
use foretoken_model_server::backend::vllm::config::{MODEL_GROUP_UID_ENV, RuntimeConfig};
use foretoken_model_server::backend::vllm::kv_events::KvEventAdapter;
use foretoken_model_server::backend::vllm::prefix_lookup;
use foretoken_model_server::backend::vllm::startup::{self, ConnectedEngine};
use foretoken_model_server::backend::vllm::transport::LOOPBACK_HOST;
use foretoken_model_server::engine::ManagedEngine;
use foretoken_model_server::execution::{InstanceAdmissionWatcher, RuntimeHealth};
use foretoken_model_server::{profiling, runtime_cache, shutdown_signal};
use futures::future::BoxFuture;
use tokio::net::TcpListener;
use tokio::sync::watch;
use tokio_util::sync::CancellationToken;
use tracing::{error, info, warn};
use vllm_engine_core_client::EngineCoreClient;
use vllm_llm::Llm;

const KV_KEY_PATH_ENV: &str = "FORETOKEN_KV_INDEX_KEY_PATH";
const KV_SCOPE_ENV: &str = "FORETOKEN_KV_SCOPE_ID";

/// One leader owns its child, HTTP future, live limits, and diagnostic tasks through shutdown.
pub(super) struct Leader {
    // Unwinding releases HTTP and diagnostic handles before their backend and process owner.
    server: BoxFuture<'static, io::Result<()>>,
    profiler: Option<profiling::Supervisor>,
    backend: Arc<VllmBackend>,
    client_health: watch::Receiver<bool>,
    engine: ManagedEngine,
    instance_admission: Option<InstanceAdmissionWatcher>,
    health: Arc<RuntimeHealth>,
    shutdown: CancellationToken,
    cache_server: Option<tokio::task::JoinHandle<io::Result<()>>>,
    cache_shutdown: CancellationToken,
    config: RuntimeConfig,
}

enum Stop {
    Signal,
    Engine(String),
    Server(String),
    CacheServer(String),
    Profiling(String),
}

impl Leader {
    /// Bind ingress ownership, connect the engine, and prepare HTTP without polling intake yet.
    pub(super) async fn start(
        config: RuntimeConfig,
        cache_config: Option<runtime_cache::Config>,
        profiling_config: Option<profiling::Config>,
        request_ledger: Option<Arc<foretoken_request_ledger::RequestLedger>>,
        mut cache_server: Option<tokio::task::JoinHandle<io::Result<()>>>,
        cache_shutdown: CancellationToken,
        background: &super::BackgroundTasks,
    ) -> Result<Self, Box<dyn std::error::Error>> {
        // Validate initial limits before engine allocation. The same watcher adopts later updates.
        let health = Arc::new(RuntimeHealth::new());
        health.set_startup_admission_limit(config.startup_admission_limit);
        let instance_admission = config
            .instance_admission_path
            .clone()
            .map(|path| InstanceAdmissionWatcher::load(path, health.clone()))
            .transpose()?;

        // Bind before ledger recovery so a second process cannot retire a live ingress's reservations.
        let listener = TcpListener::bind(config.listen_address).await?;
        let execution_store = if let Some(ledger) = request_ledger {
            let pod_uid = super::required_env("FORETOKEN_POD_UID")?;
            let epoch = ledger.register_backend(&pod_uid).await?;
            Some((ledger, pod_uid, epoch))
        } else {
            None
        };
        let kv_events = match kv_event_adapter(&config) {
            Ok(adapter) => Some(adapter),
            Err(error) => {
                warn!(%error, "KV index configuration is unavailable; prefix scoring is disabled");
                None
            }
        };

        // Connection and the optional temporary-cache retry share this complete startup budget.
        let startup_deadline = Instant::now() + config.launch.startup_timeout();
        let ConnectedEngine {
            engine,
            client,
            cache_mode,
            prepared_tokenizer,
        } = startup::start(
            &config,
            cache_config.as_ref(),
            profiling_config.as_ref(),
            startup_deadline,
            &mut cache_server,
        )
        .await?;
        health.set_process_alive(true);
        let client_health = client.subscribe_health();
        let (max_concurrent_requests, metadata) =
            runtime_metadata(&config, &client, prepared_tokenizer)?;
        let kv_events =
            start_kv_events(kv_events, &config, &client, startup_deadline, background).await;
        if !*client_health.borrow() {
            let reason = client.health_error().map_or_else(
                || "EngineCore client became unhealthy during startup".to_string(),
                |error| format!("EngineCore client became unhealthy during startup: {error}"),
            );
            warn!(%reason, "EngineCore startup failed; stopping the attempt");
            if let Err(error) = client.shutdown().await {
                warn!(%error, "could not shut down EngineCore client cleanly");
            }
            if let Err(error) = engine.shutdown(config.launch.drain_timeout()).await {
                return Err(io::Error::other(format!(
                    "{reason}; engine cleanup also failed: {error}"
                ))
                .into());
            }
            health.set_process_alive(false);
            return Err(io::Error::other(reason).into());
        }
        health.set_client_healthy(true);
        health.set_accepting(true);
        let shared_kv = if config.launch.shared_prefix_lookup() {
            Some(prefix_lookup::SharedKvLookup::new(
                super::required_env(MODEL_GROUP_UID_ENV)?,
                super::required_env(KV_SCOPE_ENV)?,
                &config,
            ))
        } else {
            None
        };
        let backend = Arc::new(VllmBackend::new(
            Llm::new(client),
            max_concurrent_requests,
            kv_events,
            shared_kv,
        ));
        let profiling = if cache_mode == runtime_cache::Mode::Persistent {
            profiling_config
        } else {
            None
        };
        let profiler =
            profiling.map(|config| profiling::Supervisor::new(config, Some(backend.clone())));

        // HTTP borrows the same health and execution owners; creating its future does not accept traffic.
        let shutdown = CancellationToken::new();
        let server_shutdown = shutdown.clone();
        let mut app_state = AppState::new(backend.clone(), health.clone(), metadata);
        if let Some((ledger, pod_uid, epoch)) = execution_store {
            app_state = app_state.with_request_ledger(ledger, pod_uid, epoch);
        }
        if let Some(profiler) = &profiler {
            app_state = app_state.with_profiling(profiler.handle());
        }
        if let Some(cache_config) = cache_config {
            app_state = app_state.with_runtime_cache(cache_config);
        }
        let server = Box::pin(
            axum::serve(
                listener,
                router(
                    app_state,
                    config.launch.internal_generate_request_body_limit_bytes,
                ),
            )
            .with_graceful_shutdown(server_shutdown.cancelled_owned())
            .into_future(),
        );
        Ok(Self {
            config,
            engine,
            backend,
            health,
            client_health,
            instance_admission,
            profiler,
            server,
            shutdown,
            cache_server,
            cache_shutdown,
        })
    }

    /// Serve until a signal or owned task fails, then retire accepted work through one drain path.
    pub(super) async fn serve(mut self) -> Result<(), Box<dyn std::error::Error>> {
        let mut stop = self.wait_for_stop().await;
        match &stop {
            Stop::Signal => info!("received shutdown signal"),
            Stop::Engine(reason)
            | Stop::Server(reason)
            | Stop::CacheServer(reason)
            | Stop::Profiling(reason) => warn!(%reason),
        }
        if let Err(cleanup) = self.drain(&mut stop).await {
            return Err(match stop {
                Stop::Signal => cleanup,
                Stop::Engine(reason)
                | Stop::Server(reason)
                | Stop::CacheServer(reason)
                | Stop::Profiling(reason) => {
                    io::Error::other(format!("{reason}; runtime cleanup also failed: {cleanup}"))
                }
            }
            .into());
        }
        match stop {
            Stop::Signal => Ok(()),
            Stop::Engine(reason)
            | Stop::Server(reason)
            | Stop::CacheServer(reason)
            | Stop::Profiling(reason) => Err(io::Error::other(reason).into()),
        }
    }

    /// Refresh projected admission before first intake, then supervise every serving lifetime together.
    async fn wait_for_stop(&mut self) -> Stop {
        let instance_admission = self.instance_admission.take();
        tokio::select! {
            biased;
            never = async move {
                match instance_admission {
                    Some(watcher) => watcher.run().await,
                    None => std::future::pending::<std::convert::Infallible>().await,
                }
            } => match never {},
            reason = engine_failure(&self.engine, &self.health, &mut self.client_health) => Stop::Engine(reason),
            signal = shutdown_signal() => match signal {
                Ok(()) => Stop::Signal,
                Err(error) => Stop::Server(format!("shutdown signal handler failed: {error}")),
            },
            result = &mut self.server => Stop::Server(match result {
                Ok(()) => "HTTP server stopped unexpectedly".into(),
                Err(error) => format!("HTTP server failed: {error}"),
            }),
            reason = runtime_cache::wait_for_server(&mut self.cache_server) => Stop::CacheServer(reason),
            result = async {
                match &mut self.profiler {
                    Some(profiler) => profiler.run().await,
                    None => std::future::pending().await,
                }
            } => Stop::Profiling(result.err().unwrap_or_else(|| "capture supervisor stopped".into())),
        }
    }

    /// Close admission and drain accepted executions while continuing to observe native process failure.
    async fn drain(&mut self, stop: &mut Stop) -> io::Result<()> {
        if let Some(profiler) = &self.profiler {
            profiler.handle().close();
        }
        self.health.set_accepting(false);
        self.health.set_client_healthy(false);
        self.shutdown.cancel();
        self.cache_shutdown.cancel();
        let deadline = Instant::now() + self.config.launch.drain_timeout();
        // Preparing diagnostics alone must not force-stop requests; only an active native capture does.
        let mut engine_stopped = matches!(stop, Stop::Engine(_))
            || self
                .profiler
                .as_ref()
                .is_some_and(|profiler| profiler.has_native_capture());
        if engine_stopped {
            stop_failed_engine(&self.engine, &self.backend, &self.health, deadline).await?;
            if let Some(profiler) = &mut self.profiler {
                profiler.engine_stopped("runtime terminated").await;
            }
        }
        let server_stopped = matches!(stop, Stop::Server(_));
        let server = &mut self.server;
        let health = &self.health;
        let drain = async {
            if !server_stopped && let Err(error) = server.as_mut().await {
                error!(%error, "HTTP server failed while draining");
            }
            health.wait_drained().await;
        };
        tokio::pin!(drain);
        loop {
            tokio::select! {
                biased;
                reason = engine_failure(&self.engine, &self.health, &mut self.client_health), if !engine_stopped => {
                    warn!(%reason, "engine failed while draining");
                    if matches!(stop, Stop::Signal) {
                        *stop = Stop::Engine(reason);
                    }
                    stop_failed_engine(&self.engine, &self.backend, &self.health, deadline).await?;
                    engine_stopped = true;
                }
                result = tokio::time::timeout_at(deadline.into(), &mut drain) => {
                    if result.is_err() {
                        warn!("accepted requests did not finish before the drain deadline");
                        stop_failed_engine(&self.engine, &self.backend, &self.health, deadline).await?;
                    }
                    break;
                }
            }
        }
        if let Err(error) = self.backend.shutdown().await {
            warn!(%error, "could not shut down EngineCore client cleanly");
        }
        let remaining = deadline.saturating_duration_since(Instant::now());
        self.engine
            .shutdown(remaining)
            .await
            .map_err(io::Error::other)?;
        self.health.set_process_alive(false);
        if let Some(profiler) = &mut self.profiler {
            profiler.engine_stopped("runtime terminated").await;
        }
        Ok(())
    }
}

/// Project engine-ready facts into the metadata and acceptance capacity exposed by this ingress.
fn runtime_metadata(
    config: &RuntimeConfig,
    client: &EngineCoreClient,
    prepared_tokenizer: Option<PreparedTokenizer>,
) -> io::Result<(Option<u64>, RuntimeMetadataResponse)> {
    let max_concurrent_requests =
        client
            .ready_responses()
            .into_iter()
            .try_fold(Some(0_u64), |total, ready| {
                let (Some(total), Some(limit)) = (total, ready.max_num_seqs) else {
                    return Ok(None);
                };
                total
                    .checked_add(limit)
                    .map(Some)
                    .ok_or_else(|| io::Error::other("EngineCore max_num_seqs sum overflowed"))
            })?;
    let metadata = RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: config.launch.artifacts.model.clone(),
            revision: config.launch.artifacts.revision.clone(),
        },
        model_dtype: client.reported_model_dtype(),
        effective_max_model_len: client.max_model_len(),
        max_logprobs: client
            .ready_responses()
            .first()
            .and_then(|ready| ready.max_logprobs),
        ec_transfer: config.launch.ec.runtime_metadata(),
        prepared_tokenizer,
        capabilities: if config.launch.ec.enabled() {
            ["ec_transfer".into()].into_iter().collect()
        } else {
            Default::default()
        },
    };
    Ok((max_concurrent_requests, metadata))
}

/// Start optional KV observation after the publisher is ready; its failure leaves generation available.
async fn start_kv_events(
    adapter: Option<Arc<KvEventAdapter>>,
    config: &RuntimeConfig,
    client: &EngineCoreClient,
    startup_deadline: Instant,
    background: &super::BackgroundTasks,
) -> Option<Arc<KvEventAdapter>> {
    let adapter = adapter?;
    let remaining = startup_deadline.saturating_duration_since(Instant::now());
    match tokio::time::timeout(remaining, adapter.load_cache_groups(client)).await {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            warn!(%error, "KV cache group metadata is unavailable; event-index queries cannot select a cache group");
        }
        Err(_) => warn!("KV cache group metadata exceeded the engine startup deadline"),
    }
    let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
    let host = config.member.as_ref().map_or_else(
        || LOOPBACK_HOST.to_string(),
        |member| member.address.to_string(),
    );
    let events = adapter.clone();
    let ports = config.ports;
    let stopped = background.shutdown.clone();
    background.tracker.spawn(async move {
        stopped
            .run_until_cancelled(events.serve(host, ports, ready_tx))
            .await;
    });
    if ready_rx.await != Ok(true) {
        warn!("KV event subscriber is unavailable; live engine observations remain available");
    }
    Some(adapter)
}

/// Observe fatal execution, transport or child failure during both serving and graceful drain.
async fn engine_failure(
    engine: &ManagedEngine,
    health: &RuntimeHealth,
    client_health: &mut watch::Receiver<bool>,
) -> String {
    tokio::select! {
        () = health.execution_failed() => "request termination could not be confirmed".into(),
        changed = client_health.changed() => {
            if changed.is_err() || !*client_health.borrow() {
                "EngineCore client became unhealthy".into()
            } else {
                unreachable!("EngineCore health only transitions to unhealthy")
            }
        },
        status = engine.wait_for_exit() => format!("managed EngineCore exited unexpectedly: {status}"),
    }
}

/// Establish native termination before waking streams and utilities that cannot receive a reply.
async fn stop_failed_engine(
    engine: &ManagedEngine,
    backend: &VllmBackend,
    health: &RuntimeHealth,
    deadline: Instant,
) -> io::Result<()> {
    engine
        .shutdown(deadline.saturating_duration_since(Instant::now()))
        .await
        .map_err(io::Error::other)?;
    health.set_process_alive(false);
    if let Err(error) = backend.shutdown().await {
        warn!(%error, "could not shut down EngineCore client cleanly");
    }
    Ok(())
}

/// Construct the index from controller-projected identity and keyed material before engine startup.
fn kv_event_adapter(
    config: &RuntimeConfig,
) -> Result<Arc<KvEventAdapter>, Box<dyn std::error::Error>> {
    let bytes = std::fs::read(std::env::var(KV_KEY_PATH_ENV)?)?;
    let key: [u8; 32] = bytes
        .as_slice()
        .try_into()
        .map_err(|_| "KV index key must be exactly 32 bytes")?;
    Ok(KvEventAdapter::new(
        key,
        super::required_env(KV_SCOPE_ENV)?,
        super::required_env(MODEL_GROUP_UID_ENV)?,
        config.launch.artifacts.revision.clone(),
        config.launch.parallelism.dp.try_into()?,
    ))
}
