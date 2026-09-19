// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Starts a managed local EngineCore child and serves the restricted internal API.

use std::future::IntoFuture;
use std::io;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Instant;

use foretoken_artifacts::ModelSource;
use foretoken_model_protocol::{RuntimeMetadataResponse, RuntimeModelIdentity};
use foretoken_model_server::api::{AppState, RuntimeHealth, router};
use foretoken_model_server::backend::VllmBackend;
use foretoken_model_server::config::RuntimeConfig;
use foretoken_model_server::kv_event_adapter::KvEventAdapter;
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
const MODEL_GROUP_UID_ENV: &str = "FORETOKEN_MODEL_GROUP_UID";
const TEMPORARY_MODEL_SOURCE_ROOT: &str = "/tmp/foretoken-model-source";

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    vllm_tracing::init_tracing("ForetokenModelServer");

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
        tokio::spawn(adapter.clone().serve(ready_tx));
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
    if config.launch.kv.shared_prefix_lookup() {
        app_state = app_state.with_shared_kv(shared_kv::SharedKvLookup::new(
            required_env(MODEL_GROUP_UID_ENV)?,
            required_env(KV_SCOPE_ENV)?,
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

async fn start_engine_attempt(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    mode: runtime_cache::Mode,
    startup_deadline: Instant,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
) -> Result<(ManagedEngine, EngineCoreClient), EngineStartupFailure> {
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
    if config.launch.kv.shared_prefix_lookup() {
        let mut python_paths = vec![std::path::PathBuf::from(shared_kv::PYTHON_MODULE_PATH)];
        if let Some(existing) = std::env::var_os("PYTHONPATH") {
            python_paths.extend(std::env::split_paths(&existing));
        }
        let python_path = std::env::join_paths(python_paths)
            .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
        environment.push((
            "PYTHONPATH".into(),
            python_path.to_string_lossy().into_owned(),
        ));
        environment.push((
            shared_kv::LOOKUP_ENDPOINT_ENV.into(),
            shared_kv::LOOKUP_ENDPOINT.into(),
        ));
    }
    let model_root = cache
        .map(|cache| cache.model_root(mode))
        .or_else(foretoken_artifacts::model_root)
        .unwrap_or_else(|| PathBuf::from(TEMPORARY_MODEL_SOURCE_ROOT));
    environment.extend(config.launch.source_environment(&model_root));
    let handshake_port = allocate_handshake_port(LOOPBACK_HOST)
        .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
    let mut managed_engine = config
        .launch
        .managed_engine(handshake_port)
        .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
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
    let ready_timeout = startup_deadline.saturating_duration_since(Instant::now());
    if ready_timeout.is_zero() {
        let _ = engine.shutdown(config.launch.drain_timeout()).await;
        return Err(EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed before client connection",
        )));
    }
    let client_config = EngineCoreClientConfig {
        transport_mode: TransportMode::HandshakeOwner {
            handshake_address: format!("tcp://{LOOPBACK_HOST}:{handshake_port}"),
            advertised_host: LOOPBACK_HOST.into(),
            engine_count: config.launch.parallelism.dp,
            ready_timeout,
            local_input_address: None,
            local_output_address: None,
        },
        coordinator_mode: None,
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
        (Some(0), Some(28)) => Ok(EngineCoreProtocol::V0_28),
        _ => Err(format!(
            "unsupported vLLM version `{version}`; supported versions are 0.20 through 0.28"
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
