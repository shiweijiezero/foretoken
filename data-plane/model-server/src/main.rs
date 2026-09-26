// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Manages local engine processes and the execution group's restricted internal API.

use std::future::IntoFuture;
use std::io;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use foretoken_artifacts::ModelSource;
use foretoken_model_protocol::{RuntimeMetadataResponse, RuntimeModelIdentity};
use foretoken_model_server::api::{AppState, RuntimeHealth, router};
use foretoken_model_server::backend::VllmBackend;
use foretoken_model_server::config::{MODEL_GROUP_UID_ENV, RuntimeConfig};
use foretoken_model_server::kv_event_adapter::KvEventAdapter;
use foretoken_model_server::launch::LaunchPlanV1;
use foretoken_model_server::managed_engine::ManagedEngine;
use foretoken_model_server::profiling;
use foretoken_model_server::runtime_cache;
use foretoken_model_server::runtime_transport::LOOPBACK_HOST;
use foretoken_model_server::shared_kv;
use tokio::net::TcpListener;
use tokio::sync::Notify;
use tracing::{error, info, warn};
use vllm_engine_core_client::{
    EngineCoreClient, EngineCoreClientConfig, EngineCoreProtocol, TransportMode,
};
use vllm_llm::Llm;
use vllm_managed_engine::allocate_handshake_port;

const KV_KEY_PATH_ENV: &str = "FORETOKEN_KV_INDEX_KEY_PATH";
const KV_SCOPE_ENV: &str = "FORETOKEN_KV_SCOPE_ID";
const TEMPORARY_MODEL_SOURCE_ROOT: &str = "/tmp/foretoken-model-source";

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    vllm_tracing::init_tracing("ForetokenModelServer");

    if std::env::args().nth(1).as_deref() == Some("prepare") {
        prepare_model().await?;
        return Ok(());
    }

    // Resolve the controller-owned launch plan before starting any engine or network task.
    let config = RuntimeConfig::from_env().map_err(std::io::Error::other)?;
    let cache_shutdown = Arc::new(Notify::new());
    let cache_config = runtime_cache::Config::from_env().map_err(std::io::Error::other)?;
    let profiling_config = if let Some(cache) = &cache_config {
        let workers = config.launch.parallelism.tp
            * config.launch.parallelism.pp
            * config.launch.parallelism.dp
            * config.launch.parallelism.pcp;
        Some(profiling::Config::from_runtime_cache(
            cache,
            required_env(MODEL_GROUP_UID_ENV)?,
            workers,
            config.launch.profiling.engine,
            config.launch.python_executable(),
        ))
    } else {
        None
    };
    let mut cache_server = if let Some(server_config) = cache_config.clone() {
        let address = (config.listen_address.ip(), server_config.observation_port());
        let listener = TcpListener::bind(address).await?;
        let shutdown = cache_shutdown.clone();
        Some(tokio::spawn(async move {
            runtime_cache::serve(listener, server_config, shutdown).await
        }))
    } else {
        None
    };
    if config
        .member
        .as_ref()
        .is_some_and(|member| member.index != 0)
    {
        return run_worker(
            &config,
            cache_config.as_ref(),
            profiling_config.as_ref(),
            &mut cache_server,
            cache_shutdown,
        )
        .await;
    }
    // Resolve optional KV projection state now; connect only after the engine publisher is ready.
    let kv_events = match kv_event_adapter(&config) {
        Ok(adapter) => Some(adapter),
        Err(error) => {
            warn!(%error, "KV index configuration is unavailable; prefix scoring is disabled");
            None
        }
    };

    // The model-server owns one startup deadline across the persistent attempt and one
    // Pod-scoped temporary retry, including complete teardown of a failed child process.
    let startup_deadline = Instant::now() + config.launch.startup_timeout();
    let (engine, client, cache_mode) = match start_engine_attempt(
        &config,
        cache_config.as_ref(),
        profiling_config.as_ref(),
        runtime_cache::Mode::Persistent,
        startup_deadline,
        &mut cache_server,
    )
    .await
    {
        Ok((engine, client)) => (engine, client, runtime_cache::Mode::Persistent),
        Err(EngineStartupFailure::PersistentCache { context, source }) => {
            // Distributed members restart together; a local retry would reuse stale peers.
            if config.member.is_some() {
                return Err(io::Error::new(source.kind(), format!("{context}: {source}")).into());
            }
            let cache = cache_config
                .as_ref()
                .expect("persistent cache failure requires a mounted RuntimeCache");
            warn!(
                cache_mode = runtime_cache::Mode::Temporary.as_str(),
                error = %source,
                %context,
                "persistent RuntimeCache became unavailable; retrying EngineCore with Pod-scoped temporary storage"
            );
            match start_engine_attempt(
                &config,
                Some(cache),
                profiling_config.as_ref(),
                runtime_cache::Mode::Temporary,
                startup_deadline,
                &mut cache_server,
            )
            .await
            {
                Ok((engine, client)) => {
                    info!(
                        cache_mode = runtime_cache::Mode::Temporary.as_str(),
                        "EngineCore started with Pod-scoped temporary cache storage"
                    );
                    (engine, client, runtime_cache::Mode::Temporary)
                }
                Err(failure) => {
                    return Err(io::Error::other(format!(
                        "temporary RuntimeCache retry failed: {}",
                        failure.into_error()
                    ))
                    .into());
                }
            }
        }
        Err(failure) => return Err(failure.into_error().into()),
    };
    let health = Arc::new(RuntimeHealth::new());
    health.set_process_alive(true);

    let mut client_health = client.subscribe_health();
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
                    .ok_or_else(|| std::io::Error::other("EngineCore max_num_seqs sum overflowed"))
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
        capabilities: if config.launch.ec.enabled() {
            ["ec_transfer".into()].into_iter().collect()
        } else {
            Default::default()
        },
    };
    if !*client_health.borrow() {
        let reason = client.health_error().map_or_else(
            || "EngineCore client became unhealthy during startup".to_string(),
            |error| format!("EngineCore client became unhealthy during startup: {error}"),
        );
        let _ = client.shutdown().await;
        let _ = engine.shutdown(config.launch.drain_timeout()).await;
        health.set_process_alive(false);
        return Err(std::io::Error::other(reason).into());
    }
    let kv_events = if let Some(adapter) = kv_events {
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        let host = config.member.as_ref().map_or_else(
            || LOOPBACK_HOST.to_string(),
            |member| member.address.to_string(),
        );
        tokio::spawn(adapter.clone().serve(host, ready_tx));
        if ready_rx.await == Ok(true) {
            Some(adapter)
        } else {
            warn!("KV event subscriber is unavailable; prefix scoring is disabled");
            None
        }
    } else {
        None
    };
    health.set_client_healthy(true);
    health.set_accepting(true);
    let backend = Arc::new(VllmBackend::new(Llm::new(client), max_concurrent_requests));
    let profiling = if cache_mode == runtime_cache::Mode::Persistent {
        profiling_config
    } else {
        None
    };
    let mut profiler =
        profiling.map(|config| profiling::Supervisor::new(config, backend.clone(), health.clone()));

    // Expose only the restricted group-local API after EngineCore is connected and healthy.
    let listener = match TcpListener::bind(config.listen_address).await {
        Ok(listener) => listener,
        Err(error) => {
            health.set_accepting(false);
            health.set_client_healthy(false);
            let _ = backend.shutdown().await;
            let _ = engine.shutdown(config.launch.drain_timeout()).await;
            return Err(error.into());
        }
    };
    let shutdown = Arc::new(Notify::new());
    let server_shutdown = shutdown.clone();
    let mut app_state = AppState::new(backend.clone(), health.clone(), metadata);
    if let Some(profiler) = &profiler {
        app_state = app_state.with_profiling(profiler.handle());
    }
    if let Some(kv_events) = kv_events {
        app_state = app_state.with_kv_events(kv_events);
    }
    if config.launch.shared_prefix_lookup() {
        app_state = app_state.with_shared_kv(shared_kv::SharedKvLookup::new(
            required_env(MODEL_GROUP_UID_ENV)?,
            required_env(KV_SCOPE_ENV)?,
            &config,
        ));
    }
    if let Some(cache_config) = cache_config {
        app_state = app_state.with_runtime_cache(cache_config);
    }
    let mut server = Box::pin(
        axum::serve(
            listener,
            router(
                app_state,
                config.launch.internal_generate_request_body_limit_bytes,
            ),
        )
        .with_graceful_shutdown(async move { server_shutdown.notified().await })
        .into_future(),
    );

    // Signals, client health, child exit, and server failure converge on one shutdown path.
    enum Stop {
        Signal,
        ClientUnhealthy(String),
        ChildExited(String),
        Server(String),
        CacheServer(String),
        Profiling(String),
    }
    let stop = tokio::select! {
        () = shutdown_signal() => Stop::Signal,
        changed = client_health.changed() => {
            if changed.is_err() || !*client_health.borrow() {
                Stop::ClientUnhealthy("EngineCore client became unhealthy".into())
            } else {
                unreachable!("EngineCore health only transitions to unhealthy")
            }
        },
        status = engine.wait_for_exit() => Stop::ChildExited(format!("managed EngineCore exited unexpectedly: {status}")),
        result = &mut server => Stop::Server(match result {
            Ok(()) => "HTTP server stopped unexpectedly".into(),
            Err(error) => format!("HTTP server failed: {error}"),
        }),
        reason = wait_cache_server(&mut cache_server) => Stop::CacheServer(reason),
        result = async {
            match &mut profiler {
                Some(profiler) => profiler.run().await,
                None => std::future::pending().await,
            }
        } => Stop::Profiling(result.err().unwrap_or_else(|| "capture supervisor stopped".into())),
    };
    match &stop {
        Stop::Signal => info!("received shutdown signal"),
        Stop::ClientUnhealthy(reason)
        | Stop::ChildExited(reason)
        | Stop::Server(reason)
        | Stop::CacheServer(reason)
        | Stop::Profiling(reason) => warn!(%reason),
    }

    // Stop new admission before draining HTTP handlers, the client, and finally the child process.
    health.set_accepting(false);
    health.set_client_healthy(false);
    shutdown.notify_waiters();
    cache_shutdown.notify_waiters();
    let deadline = Instant::now() + config.launch.drain_timeout();
    // Only an active native capture can hold the backend lock or require forced profiler stop.
    // Merely preparing profiling must not bypass normal request draining on SIGTERM.
    if let Some(profiler) = &mut profiler
        && profiler.has_native_capture()
    {
        engine.shutdown(config.launch.drain_timeout()).await?;
        engine.wait_for_exit().await;
        profiler
            .engine_stopped("diagnostic runtime terminated")
            .await;
    }
    if !matches!(&stop, Stop::Server(_)) {
        match tokio::time::timeout(
            deadline.saturating_duration_since(Instant::now()),
            server.as_mut(),
        )
        .await
        {
            Ok(Ok(())) => {}
            Ok(Err(error)) => error!(%error, "HTTP server failed while draining"),
            Err(_) => {
                warn!("HTTP handlers did not drain before deadline");
                drop(server);
            }
        }
    }
    if let Err(error) = backend.shutdown().await {
        warn!(%error, "could not shut down EngineCore client cleanly");
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    engine.shutdown(remaining).await.map_err(io::Error::other)?;
    health.set_process_alive(false);
    if let Some(profiler) = &mut profiler {
        profiler.engine_stopped("runtime terminated").await;
    }

    match stop {
        Stop::Signal => Ok(()),
        Stop::ClientUnhealthy(reason)
        | Stop::ChildExited(reason)
        | Stop::Server(reason)
        | Stop::CacheServer(reason)
        | Stop::Profiling(reason) => Err(std::io::Error::other(reason).into()),
    }
}

// Workers own only their local child and cache observer. Collective readiness is
// established by the leader's handshake with every EngineCore before Group admission.
async fn run_worker(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
    cache_shutdown: Arc<Notify>,
) -> Result<(), Box<dyn std::error::Error>> {
    let listener = TcpListener::bind(config.listen_address).await?;
    let (engine, _, _) = spawn_engine_attempt(
        config,
        cache,
        profiling,
        runtime_cache::Mode::Persistent,
        Instant::now() + config.launch.startup_timeout(),
    )
    .await
    .map_err(EngineStartupFailure::into_error)?;
    let shutdown = Arc::new(Notify::new());
    let server_shutdown = shutdown.clone();
    let app = axum::Router::new()
        .route(
            "/healthz",
            axum::routing::get(|| async { axum::http::StatusCode::OK }),
        )
        .route(
            "/readyz",
            axum::routing::get(|| async { axum::http::StatusCode::OK }),
        );
    let mut server = Box::pin(
        axum::serve(listener, app)
            .with_graceful_shutdown(async move { server_shutdown.notified().await })
            .into_future(),
    );
    let failure = tokio::select! {
        () = shutdown_signal() => None,
        status = engine.wait_for_exit() => Some(format!("managed worker exited: {status}")),
        result = &mut server => Some(format!("worker health server stopped: {result:?}")),
        reason = wait_cache_server(cache_server) => Some(reason),
        error = wait_cache_write_failure(cache, runtime_cache::Mode::Persistent) => Some(error.to_string()),
    };
    shutdown.notify_waiters();
    cache_shutdown.notify_waiters();
    engine.shutdown(config.launch.drain_timeout()).await?;
    match failure {
        Some(message) => Err(io::Error::other(message).into()),
        None => Ok(()),
    }
}

enum EngineStartupFailure {
    PersistentCache { context: String, source: io::Error },
    Other(io::Error),
}

impl EngineStartupFailure {
    fn into_error(self) -> io::Error {
        match self {
            Self::PersistentCache { context, source } => {
                io::Error::new(source.kind(), format!("{context}: {source}"))
            }
            Self::Other(error) => error,
        }
    }
}

fn cache_mode_failure(
    mode: runtime_cache::Mode,
    context: impl Into<String>,
    source: io::Error,
) -> EngineStartupFailure {
    let context = context.into();
    if mode == runtime_cache::Mode::Persistent {
        EngineStartupFailure::PersistentCache { context, source }
    } else {
        EngineStartupFailure::Other(io::Error::new(
            source.kind(),
            format!("{context}: {source}"),
        ))
    }
}

fn classify_engine_startup_failure(
    cache: Option<&runtime_cache::Config>,
    mode: runtime_cache::Mode,
    message: String,
) -> EngineStartupFailure {
    if let Some(cache) = cache
        && let Err(error) = cache.probe_writable(mode)
    {
        return cache_mode_failure(
            mode,
            format!("{message}; {} cache write probe failed", mode.as_str()),
            error,
        );
    }
    EngineStartupFailure::Other(io::Error::other(message))
}

async fn wait_cache_write_failure(
    cache: Option<&runtime_cache::Config>,
    mode: runtime_cache::Mode,
) -> io::Error {
    let Some(cache) = cache else {
        return std::future::pending().await;
    };
    cache.wait_until_unwritable(mode).await
}

// Prepare cache, profiling and native arguments once for both leader and worker processes.
async fn spawn_engine_attempt(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    mode: runtime_cache::Mode,
    startup_deadline: Instant,
) -> Result<(ManagedEngine, EngineCoreProtocol, u16), EngineStartupFailure> {
    let mut environment = if let Some(cache) = cache {
        cache.set_mode(mode);
        cache
            .prepare(mode)
            .map_err(|error| cache_mode_failure(mode, "cache preparation failed", error))?;
        cache.engine_environment(mode)
    } else {
        Vec::new()
    };
    if mode == runtime_cache::Mode::Persistent
        && let Some(profile) = profiling
    {
        profile.prepare().map_err(|error| {
            cache_mode_failure(mode, "profiling storage preparation failed", error)
        })?;
    }
    if config.launch.shared_prefix_lookup()
        || config.launch.ec.enabled()
        || config.launch.profiling.engine == profiling::Engine::Mctracer
    {
        let mut python_paths = vec![PathBuf::from(
            foretoken_model_server::launch::PYTHON_MODULE_PATH,
        )];
        if let Some(existing) = std::env::var_os("PYTHONPATH") {
            python_paths.extend(std::env::split_paths(&existing));
        }
        let python_path = std::env::join_paths(python_paths)
            .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
        environment.push((
            "PYTHONPATH".into(),
            python_path.to_string_lossy().into_owned(),
        ));
    }
    if config.launch.shared_prefix_lookup() {
        environment.push((
            shared_kv::LOOKUP_ENDPOINT_ENV.into(),
            shared_kv::lookup_endpoint("*", 0),
        ));
    }
    let model_root = cache
        .map(|cache| cache.model_root(mode))
        .or_else(foretoken_artifacts::model_root)
        .unwrap_or_else(|| PathBuf::from(TEMPORARY_MODEL_SOURCE_ROOT));
    environment.extend(config.launch.source_environment(&model_root));
    let handshake_port = if config.member.is_some() {
        29700
    } else {
        allocate_handshake_port(LOOPBACK_HOST)
            .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?
    };
    let mut managed_engine = config
        .launch
        .managed_engine(handshake_port, config.member.as_ref())
        .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
    if let Some(member) = &config.member {
        environment.push(("VLLM_HOST_IP".into(), member.address.to_string()));
    }
    // Local source is strict: both identifiers must resolve before any engine process starts.
    if config.launch.artifacts.source == ModelSource::Local {
        let model = local_artifact_path(&config.launch.artifacts.model)
            .map_err(EngineStartupFailure::Other)?;
        let tokenizer = local_artifact_path(&config.launch.artifacts.tokenizer)
            .map_err(EngineStartupFailure::Other)?;
        managed_engine.model = model;
        for argument in &mut managed_engine.python_args {
            if argument.starts_with("--tokenizer=") {
                *argument = format!("--tokenizer={tokenizer}");
            }
        }
    }
    if mode == runtime_cache::Mode::Persistent
        && let Some(profile) = profiling
    {
        managed_engine.python_args.push(profile.engine_argument());
        if config.launch.profiling.engine == profiling::Engine::Mctracer {
            managed_engine
                .python_args
                .push("--worker-cls=foretoken_mctracer.Worker".into());
        }
    }
    let protocol_timeout = startup_deadline.saturating_duration_since(Instant::now());
    if protocol_timeout.is_zero() {
        return Err(EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed before version detection",
        )));
    }
    let engine_protocol = tokio::time::timeout(
        protocol_timeout,
        detect_engine_protocol(&managed_engine.python, &environment),
    )
    .await
    .map_err(|_| {
        EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed during version detection",
        ))
    })?
    .map_err(|error| classify_engine_startup_failure(cache, mode, format!("{error}")))?;
    // Rust preprocessing already normalizes pixels. Newer engines otherwise normalize them twice.
    if matches!(engine_protocol, EngineCoreProtocol::V0_28ToV0_30) {
        managed_engine
            .python_args
            .push("--no-mm-device-do-normalize".into());
    }
    let mut command = managed_engine.to_command();
    command.envs(environment);
    let instrumentation = profiling.filter(|_| mode == runtime_cache::Mode::Persistent);
    let engine = ManagedEngine::spawn(command, instrumentation)
        .await
        .map_err(|error| {
            classify_engine_startup_failure(
                cache,
                mode,
                format!("could not spawn managed EngineCore: {error}"),
            )
        })?;
    Ok((engine, engine_protocol, handshake_port))
}

// Only the group leader owns the EngineCore handshake, DP coordination and request transport.
async fn start_engine_attempt(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    mode: runtime_cache::Mode,
    startup_deadline: Instant,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
) -> Result<(ManagedEngine, EngineCoreClient), EngineStartupFailure> {
    let (engine, engine_protocol, handshake_port) =
        spawn_engine_attempt(config, cache, profiling, mode, startup_deadline).await?;
    // vLLM keeps its single EngineCore handshake local even when TP/PP spans nodes.
    let advertised_host = config
        .member
        .as_ref()
        .filter(|_| config.launch.parallelism.dp > 1)
        .map_or_else(
            || LOOPBACK_HOST.to_string(),
            |member| member.address.to_string(),
        );
    let ready_timeout = startup_deadline.saturating_duration_since(Instant::now());
    if ready_timeout.is_zero() {
        let _ = engine.shutdown(config.launch.drain_timeout()).await;
        return Err(EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed before client connection",
        )));
    }
    let client_config = EngineCoreClientConfig {
        transport_mode: TransportMode::HandshakeOwner {
            handshake_address: format!("tcp://{advertised_host}:{handshake_port}"),
            advertised_host,
            engine_count: config.launch.parallelism.dp,
            ready_timeout,
            local_input_address: None,
            local_output_address: None,
        },
        coordinator_mode: (config.launch.parallelism.dp > 1)
            .then_some(vllm_engine_core_client::CoordinatorMode::InProc),
        model_name: config.launch.artifacts.model.clone(),
        client_index: 0,
    };
    let client = tokio::select! {
        result = EngineCoreClient::connect_with_protocol(client_config, engine_protocol) => match result {
            Ok(client) => Ok(client),
            Err(error) => Err(classify_engine_startup_failure(cache, mode, format!("could not connect to EngineCore: {error}"))),
        },
        status = engine.wait_for_exit() => Err(classify_engine_startup_failure(cache, mode, format!("managed EngineCore exited during startup: {status}"))),
        reason = wait_cache_server(cache_server) => Err(EngineStartupFailure::Other(io::Error::other(format!("cache observation server stopped during startup: {reason}")))),
        error = wait_cache_write_failure(cache, mode) => Err(cache_mode_failure(mode, format!("{} cache became unwritable during EngineCore startup", mode.as_str()), error)),
    };
    match client {
        Ok(client) => Ok((engine, client)),
        Err(error) => {
            // Failed cleanup must end this runtime, not start a temporary-cache retry beside the old engine.
            engine
                .shutdown(config.launch.drain_timeout())
                .await
                .map_err(|reason| EngineStartupFailure::Other(io::Error::other(reason)))?;
            Err(error)
        }
    }
}

fn local_artifact_path(identifier: &str) -> io::Result<String> {
    let root = foretoken_artifacts::model_root();
    let path =
        foretoken_artifacts::resolve_directory(root.as_deref(), identifier)?.ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                format!("local artifact {identifier:?} was not found"),
            )
        })?;
    path.into_os_string().into_string().map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "local artifact path is not UTF-8",
        )
    })
}

async fn wait_cache_server(server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>) -> String {
    let Some(server) = server else {
        return std::future::pending().await;
    };
    match server.await {
        Ok(Ok(())) => "cache observation server stopped unexpectedly".into(),
        Ok(Err(error)) => format!("cache observation server failed: {error}"),
        Err(error) => format!("cache observation task failed: {error}"),
    }
}

/// Select protocol layouts from installed package metadata without loading engine plugins.
async fn detect_engine_protocol(
    python: &str,
    environment: &[(String, String)],
) -> Result<EngineCoreProtocol, Box<dyn std::error::Error>> {
    let output = tokio::process::Command::new(python)
        .envs(environment.iter().cloned())
        .args([
            "-c",
            "from importlib.metadata import version; print(version('vllm'))",
        ])
        .output()
        .await?;
    if !output.status.success() {
        return Err(format!("could not inspect vLLM version using {python}").into());
    }
    let version = String::from_utf8(output.stdout)?.trim().to_owned();
    let mut parts = version.split('.');
    let major = parts.next().and_then(|part| part.parse::<u64>().ok());
    let minor = parts.next().and_then(|part| part.parse::<u64>().ok());
    match (major, minor) {
        (Some(0), Some(20)) => Ok(EngineCoreProtocol::V0_20),
        (Some(0), Some(21..=25)) => Ok(EngineCoreProtocol::V0_21ToV0_25),
        (Some(0), Some(26..=27)) => Ok(EngineCoreProtocol::V0_26ToV0_27),
        (Some(0), Some(28..=30)) => Ok(EngineCoreProtocol::V0_28ToV0_30),
        _ => Err(format!(
            "unsupported vLLM version `{version}`; supported versions are 0.20 through 0.30"
        )
        .into()),
    }
}

// Build the adapter only from controller-projected identity and keyed material. Startup owns the
// returned `Arc` until it either launches the subscriber task or omits the KV index endpoint.
fn kv_event_adapter(
    config: &RuntimeConfig,
) -> Result<Arc<KvEventAdapter>, Box<dyn std::error::Error>> {
    let bytes = std::fs::read(std::env::var(KV_KEY_PATH_ENV)?)?;
    let key: [u8; 32] = bytes
        .as_slice()
        .try_into()
        .map_err(|_| "KV index key must be exactly 32 bytes")?;
    let scope_id = required_env(KV_SCOPE_ENV)?;
    let model_group_id = required_env(MODEL_GROUP_UID_ENV)?;
    Ok(KvEventAdapter::new(
        key,
        scope_id,
        model_group_id,
        config.launch.artifacts.revision.clone(),
        config.launch.parallelism.dp.try_into()?,
    ))
}

/// Prepares a model source in the mounted cache before the engine container starts.
///
/// ModelGroup init containers invoke this mode with the same launch plan and cache mount as the
/// serving process. The provider owns its cache layout and resumability; this process only selects
/// the provider call and reports its exit status.
async fn prepare_model() -> Result<(), Box<dyn std::error::Error>> {
    let plan = LaunchPlanV1::parse(&required_env("FORETOKEN_VLLM_LAUNCH_PLAN")?)?;
    let model_root = foretoken_artifacts::model_root()
        .ok_or("FORETOKEN_MODEL_ROOT must be set for model preparation")?;
    let python = std::env::var("FORETOKEN_VLLM_PYTHON")
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "python".into());
    let source = match plan.artifacts.source {
        ModelSource::Hf => "hf",
        ModelSource::ModelScope => "modelscope",
        ModelSource::Local => return Ok(()),
    };
    let code = r#"
import os

source = os.environ["FORETOKEN_PREPARE_SOURCE"]
model = (os.environ["FORETOKEN_PREPARE_MODEL"], os.environ["FORETOKEN_PREPARE_MODEL_REVISION"])
tokenizer = (os.environ["FORETOKEN_PREPARE_TOKENIZER"], os.environ["FORETOKEN_PREPARE_TOKENIZER_REVISION"])
artifacts = [model] if tokenizer == model else [model, tokenizer]
if source == "hf":
    from huggingface_hub import snapshot_download
    for repository, revision in artifacts:
        snapshot_download(repo_id=repository, revision=revision)
elif source == "modelscope":
    from modelscope import snapshot_download
    for repository, revision in artifacts:
        snapshot_download(model_id=repository, revision=revision, cache_dir=os.environ["MODELSCOPE_CACHE"])
else:
    raise RuntimeError(f"unsupported model preparation source: {source}")
"#;
    let mut command = tokio::process::Command::new(python);
    command
        .args(["-c", code])
        .env("FORETOKEN_PREPARE_SOURCE", source)
        .env("FORETOKEN_PREPARE_MODEL", &plan.artifacts.model)
        .env("FORETOKEN_PREPARE_MODEL_REVISION", &plan.artifacts.revision)
        .env("FORETOKEN_PREPARE_TOKENIZER", &plan.artifacts.tokenizer)
        .env(
            "FORETOKEN_PREPARE_TOKENIZER_REVISION",
            &plan.artifacts.tokenizer_revision,
        )
        .env(foretoken_artifacts::MODEL_ROOT_ENV, &model_root)
        .env("HF_HOME", &model_root)
        .env("HF_HUB_CACHE", model_root.join("hub"))
        .env(
            foretoken_artifacts::MODELSCOPE_CACHE_ENV,
            foretoken_artifacts::modelscope_cache_root(&model_root),
        );
    let status = command.status().await?;
    if !status.success() {
        return Err(format!("model preparation failed with status {status}").into());
    }
    Ok(())
}

fn required_env(name: &str) -> Result<String, Box<dyn std::error::Error>> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{name} must be set by the ModelGroup controller").into())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("installing Ctrl-C signal handler must succeed");
    };
    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("installing SIGTERM signal handler must succeed")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! { () = ctrl_c => {}, () = terminate => {} }
}
