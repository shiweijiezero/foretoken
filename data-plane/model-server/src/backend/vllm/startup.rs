// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Starts one engine attempt, owns its connection deadline, and classifies cache retry failures.

use std::io;
use std::path::PathBuf;
use std::time::Instant;

use crate::backend::vllm::config::RuntimeConfig;
use crate::backend::vllm::launch::{KvPlan, PdProtocol};
use crate::backend::vllm::prefix_lookup;
use crate::backend::vllm::transport::LOOPBACK_HOST;
use crate::engine::{ManagedEngine, PYTHON_MODULE_PATH};
use crate::model_cache::preparation;
use crate::{profiling, runtime_cache};
use foretoken_artifacts::ModelSource;
use foretoken_model_protocol::{KvConnector, PreparedTokenizer, RuntimeKvTransferMetadata};
use tracing::{info, warn};
use vllm_engine_core_client::{
    EngineCoreClient, EngineCoreClientConfig, EngineCoreProtocol, TransportMode,
};
use vllm_managed_engine::ManagedEngineConfig;

/// A connected leader's process and transport, retained together through serving and shutdown.
pub struct ConnectedEngine {
    pub engine: ManagedEngine,
    pub client: EngineCoreClient,
    pub cache_mode: runtime_cache::Mode,
    pub prepared_tokenizer: Option<PreparedTokenizer>,
    pub kv_transfer: Option<RuntimeKvTransferMetadata>,
    // The adapter still drains ordinary P/D transfers when the active layout disables reuse.
    pub kv_connector_utility: bool,
}

/// A spawned member before the leader establishes request transport.
pub struct SpawnedEngine {
    pub engine: ManagedEngine,
    protocol: EngineCoreProtocol,
    handshake_port: u16,
    prepared_tokenizer: Option<PreparedTokenizer>,
    kv_transfer: Option<RuntimeKvTransferMetadata>,
}

/// Package and device facts consumed before one engine process loads its native libraries.
struct VllmInspection {
    protocol: EngineCoreProtocol,
    retained_prefixes: bool,
    model_express_available: bool,
    mooncake_dma_buf: bool,
}

/// Startup error that distinguishes a retryable persistent-cache failure from other failures.
pub enum EngineStartupFailure {
    PersistentCache { context: String, source: io::Error },
    Other(io::Error),
}

impl EngineStartupFailure {
    /// Return the startup failure to the process supervisor, preserving its I/O error kind.
    pub fn into_error(self) -> io::Error {
        match self {
            Self::PersistentCache { context, source } => {
                io::Error::new(source.kind(), format!("{context}: {source}"))
            }
            Self::Other(error) => error,
        }
    }
}

/// Start the leader with one deadline shared by persistent storage and its single local retry.
/// A failed attempt must finish teardown before temporary storage can acquire process ownership.
pub async fn start(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    startup_deadline: Instant,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
) -> Result<ConnectedEngine, Box<dyn std::error::Error>> {
    match start_engine_attempt(
        config,
        cache,
        profiling,
        runtime_cache::Mode::Persistent,
        startup_deadline,
        cache_server,
    )
    .await
    {
        Ok(connected) => Ok(connected),
        Err(EngineStartupFailure::PersistentCache { context, source }) => {
            // Distributed members restart together; a local retry would reuse stale peers.
            if config.member.is_some() {
                return Err(io::Error::new(source.kind(), format!("{context}: {source}")).into());
            }
            let cache = cache.expect("persistent cache failure requires a mounted RuntimeCache");
            warn!(
                cache_mode = runtime_cache::Mode::Temporary.as_str(),
                error = %source,
                %context,
                "persistent RuntimeCache became unavailable; retrying EngineCore with Pod-scoped temporary storage"
            );
            match start_engine_attempt(
                config,
                Some(cache),
                profiling,
                runtime_cache::Mode::Temporary,
                startup_deadline,
                cache_server,
            )
            .await
            {
                Ok(connected) => {
                    info!(
                        cache_mode = runtime_cache::Mode::Temporary.as_str(),
                        "EngineCore started with Pod-scoped temporary cache storage"
                    );
                    Ok(connected)
                }
                Err(failure) => Err(io::Error::other(format!(
                    "temporary RuntimeCache retry failed: {}",
                    failure.into_error()
                ))
                .into()),
            }
        }
        Err(failure) => Err(failure.into_error().into()),
    }
}

/// Prepare model files, native arguments and instrumentation before starting a leader or worker.
/// The returned process stays owned by the caller even when only the leader opens a client.
pub async fn spawn_engine_attempt(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    mode: runtime_cache::Mode,
    startup_deadline: Instant,
) -> Result<SpawnedEngine, EngineStartupFailure> {
    let mut environment = prepare_environment(config, cache, profiling, mode)?;
    // Native connector options and wire protocol come from the same installed-package inspection.
    let package_timeout = startup_deadline.saturating_duration_since(Instant::now());
    if package_timeout.is_zero() {
        return Err(EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed before package inspection",
        )));
    }
    let mooncake_rdma = matches!(
        config.launch.kv,
        KvPlan::Pd {
            connector: KvConnector::Mooncake,
            protocol: PdProtocol::Rdma,
            ..
        } | KvPlan::MultiConnector {
            connector: KvConnector::Mooncake,
            protocol: PdProtocol::Rdma,
            ..
        }
    );
    let VllmInspection {
        protocol: engine_protocol,
        retained_prefixes,
        model_express_available,
        mooncake_dma_buf,
    } = tokio::time::timeout(
        package_timeout,
        inspect_engine_environment(
            &crate::engine::python_executable(),
            &environment,
            mooncake_rdma,
        ),
    )
    .await
    .map_err(|_| {
        EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed during package inspection",
        ))
    })?
    .map_err(|error| classify_engine_startup_failure(cache, mode, format!("{error}")))?;
    if mooncake_dma_buf {
        // Mooncake reads this choice when its library initializes in the engine process.
        environment.push(("WITH_NVIDIA_PEERMEM".into(), "0".into()));
    }
    let handshake_port = config.ports.handshake;
    let (mut managed_engine, kv_transfer) = config
        .launch
        .managed_engine(
            handshake_port,
            config.member.as_ref(),
            retained_prefixes,
            &config.ports,
        )
        .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
    if let Some(member) = &config.member {
        environment.push(("VLLM_HOST_IP".into(), member.address.to_string()));
    }
    let prepared_tokenizer = prepare_model_artifacts(
        config,
        cache,
        mode,
        startup_deadline,
        &mut managed_engine,
        &mut environment,
    )
    .await?;
    if mode == runtime_cache::Mode::Persistent
        && let Some(profile) = profiling
    {
        managed_engine
            .python_args
            .push(super::profiling::engine_argument(profile));
        if config.launch.profiling.engine == profiling::Engine::Mctracer {
            managed_engine
                .python_args
                .push("--worker-cls=foretoken_mctracer.Worker".into());
        } else if config.launch.profiling.engine == profiling::Engine::Pytorch {
            managed_engine
                .python_args
                .push("--worker-extension-cls=foretoken_pytorch.WorkerExtension".into());
        }
    }

    if !model_express_available {
        managed_engine
            .python_args
            .retain(|argument| argument != "--load-format=modelexpress");
    }
    // Rust preprocessing already normalizes pixels; newer engines must not normalize twice.
    if matches!(engine_protocol, EngineCoreProtocol::V0_28ToV0_31) {
        managed_engine
            .python_args
            .push("--no-mm-device-do-normalize".into());
    }
    let mut command = config.launch.engine_command(
        &managed_engine,
        config.member.as_ref(),
        &config.ports,
        startup_deadline.saturating_duration_since(Instant::now()),
    );
    command.envs(environment);
    let instrumentation = profiling.filter(|_| mode == runtime_cache::Mode::Persistent);
    if instrumentation.is_some_and(|config| config.engine == profiling::Engine::Nsight) {
        command.env("VLLM_WORKER_MULTIPROC_METHOD", "spawn");
    }
    let engine = ManagedEngine::spawn(command, instrumentation)
        .await
        .map_err(|error| {
            classify_engine_startup_failure(
                cache,
                mode,
                format!("could not spawn managed EngineCore: {error}"),
            )
        })?;
    Ok(SpawnedEngine {
        engine,
        protocol: engine_protocol,
        handshake_port,
        prepared_tokenizer,
        kv_transfer,
    })
}

/// Prepare writable cache and diagnostic storage and the corresponding child environment.
fn prepare_environment(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    mode: runtime_cache::Mode,
) -> Result<Vec<(String, String)>, EngineStartupFailure> {
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
    if config.member.is_some()
        || config.launch.shared_prefix_lookup()
        || matches!(config.launch.kv, KvPlan::Pd { .. })
        || config.launch.ec.enabled()
        || config.launch.profiling.engine == profiling::Engine::Mctracer
    {
        let mut python_paths = std::env::var_os("PYTHONPATH")
            .map(|existing| std::env::split_paths(&existing).collect::<Vec<_>>())
            .unwrap_or_default();
        if std::env::var_os(foretoken_artifacts::source::ACTIVE_DIRECTORY_ENV).is_none() {
            python_paths.insert(0, PathBuf::from(PYTHON_MODULE_PATH));
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
            prefix_lookup::LOOKUP_ENDPOINT_ENV.into(),
            config.ports.lookup_endpoint("*", 0),
        ));
    }
    Ok(environment)
}

/// Bind fixed provider publications or strict local paths to one attempt's native arguments.
/// Temporary execution may reuse readable persistent snapshots without changing their identity.
async fn prepare_model_artifacts(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    mode: runtime_cache::Mode,
    startup_deadline: Instant,
    managed_engine: &mut ManagedEngineConfig,
    environment: &mut Vec<(String, String)>,
) -> Result<Option<PreparedTokenizer>, EngineStartupFailure> {
    let model_root = cache
        .map(|cache| cache.model_cache_root(mode))
        .or_else(foretoken_artifacts::model_cache_root)
        .unwrap_or_else(|| PathBuf::from("/tmp/foretoken-model-source"));
    let preparation_root = cache
        .map(|cache| cache.preparation_root(mode))
        .or_else(foretoken_artifacts::preparation_root)
        .unwrap_or_else(|| model_root.join("prepared"));
    // Retained Pod templates keep native acquisition until their Pool adopts preparation.
    let legacy_acquisition = std::env::var_os(preparation::PREPARATION_SCOPE_ENV).is_none();
    environment.push((
        "VLLM_USE_MODELSCOPE".into(),
        (legacy_acquisition && config.launch.artifacts.source == ModelSource::ModelScope)
            .to_string(),
    ));
    if legacy_acquisition && config.launch.artifacts.source == ModelSource::ModelScope {
        environment.push((
            foretoken_artifacts::MODELSCOPE_CACHE_ENV.into(),
            foretoken_artifacts::modelscope_cache_root(&model_root)
                .display()
                .to_string(),
        ));
    }
    let mut prepared_tokenizer = None;
    if config.launch.artifacts.source != ModelSource::Local && !legacy_acquisition {
        let scope_name = preparation::PREPARATION_SCOPE_ENV;
        let scope = std::env::var(scope_name)
            .ok()
            .filter(|value| !value.is_empty())
            .ok_or_else(|| {
                EngineStartupFailure::Other(io::Error::other(format!(
                    "{scope_name} must be set by the ModelGroup controller"
                )))
            })?;
        let remaining = startup_deadline.saturating_duration_since(Instant::now());
        let persistent_root =
            cache.map(|cache| cache.model_cache_root(runtime_cache::Mode::Persistent));
        let persistent_state =
            cache.map(|cache| cache.preparation_root(runtime_cache::Mode::Persistent));
        let storage = preparation::PreparationStorage {
            model_root: &model_root,
            state_root: &preparation_root,
            reusable_roots: persistent_root
                .as_deref()
                .zip(persistent_state.as_deref())
                .filter(|(root, _)| *root != model_root),
            environment,
        };
        let prepared = tokio::time::timeout(
            remaining,
            preparation::prepare(
                &config.launch.artifacts,
                &managed_engine.python,
                &storage,
                &scope,
                true,
            ),
        )
        .await
        .map_err(|_| {
            EngineStartupFailure::Other(io::Error::other(
                "model preparation exceeded the startup deadline",
            ))
        })?
        .map_err(|error| classify_engine_startup_failure(cache, mode, error.to_string()))?;
        managed_engine.model = prepared.model.display().to_string();
        managed_engine.python_args.retain(|argument| {
            !argument.starts_with("--revision=") && !argument.starts_with("--tokenizer-revision=")
        });
        for argument in &mut managed_engine.python_args {
            if argument.starts_with("--tokenizer=") {
                *argument = format!("--tokenizer={}", prepared.tokenizer.display());
            }
        }
        let binding = persistent_root
            .as_ref()
            .filter(|root| prepared.tokenizer.starts_with(root))
            .and_then(|_| std::env::var(foretoken_artifacts::RUNTIME_CACHE_BINDING_ENV).ok());
        prepared_tokenizer = Some(prepared.tokenizer_metadata(&scope, binding));
    }
    if config.launch.artifacts.source == ModelSource::Local {
        let model = preparation::local_artifact_path(&config.launch.artifacts.model)
            .map_err(EngineStartupFailure::Other)?;
        let tokenizer = preparation::local_artifact_path(&config.launch.artifacts.tokenizer)
            .map_err(EngineStartupFailure::Other)?;
        managed_engine.model = model;
        for argument in &mut managed_engine.python_args {
            if argument.starts_with("--tokenizer=") {
                *argument = format!("--tokenizer={tokenizer}");
            }
        }
    }
    Ok(prepared_tokenizer)
}

/// Establish the leader's handshake and DP transport without exceeding the attempt deadline.
async fn start_engine_attempt(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    mode: runtime_cache::Mode,
    startup_deadline: Instant,
    cache_server: &mut Option<tokio::task::JoinHandle<io::Result<()>>>,
) -> Result<ConnectedEngine, EngineStartupFailure> {
    let SpawnedEngine {
        engine,
        protocol,
        handshake_port,
        prepared_tokenizer,
        kv_transfer,
    } = spawn_engine_attempt(config, cache, profiling, mode, startup_deadline).await?;
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
    let client = if ready_timeout.is_zero() {
        Err(EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed before client connection",
        )))
    } else {
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
        // Upstream handshake waits are per phase; this owner bounds the complete connection.
        tokio::select! {
            result = EngineCoreClient::connect_with_protocol(client_config, protocol) => match result {
                Ok(client) => Ok(client),
                Err(error) => Err(classify_engine_startup_failure(cache, mode, format!("could not connect to EngineCore: {error}"))),
            },
            status = engine.wait_for_exit() => Err(classify_engine_startup_failure(cache, mode, format!("managed EngineCore exited during startup: {status}"))),
            _ = tokio::time::sleep_until(startup_deadline.into()) => Err(EngineStartupFailure::Other(io::Error::other("EngineCore connection exceeded the startup deadline"))),
            reason = runtime_cache::wait_for_server(cache_server) => Err(EngineStartupFailure::Other(io::Error::other(format!("cache observation server stopped during startup: {reason}")))),
            error = wait_cache_write_failure(cache, mode) => Err(cache_mode_failure(mode, format!("{} cache became unwritable during EngineCore startup", mode.as_str()), error)),
        }
    };
    match client {
        Ok(client) => {
            let kv_connector_utility = kv_transfer
                .as_ref()
                .is_some_and(|kv| kv.bidirectional_kv_xfer);
            match observe_kv_transfer(&client, kv_transfer, startup_deadline).await {
                Ok(kv_transfer) => Ok(ConnectedEngine {
                    engine,
                    client,
                    cache_mode: mode,
                    prepared_tokenizer,
                    kv_transfer,
                    kv_connector_utility,
                }),
                Err(error) => {
                    let error = error.into_error();
                    warn!(%error, "EngineCore KV configuration observation failed; stopping the attempt");
                    if let Err(reason) = client.shutdown().await {
                        warn!(error = %reason, "could not shut down EngineCore client cleanly");
                    }
                    if let Err(reason) = engine.shutdown(config.launch.drain_timeout()).await {
                        return Err(EngineStartupFailure::Other(io::Error::other(format!(
                            "{error}; engine cleanup also failed: {reason}"
                        ))));
                    }
                    Err(EngineStartupFailure::Other(error))
                }
            }
        }
        Err(error) => {
            match &error {
                EngineStartupFailure::PersistentCache { context, source } => {
                    warn!(%context, error = %source, "EngineCore startup failed; stopping the attempt");
                }
                EngineStartupFailure::Other(source) => {
                    warn!(error = %source, "EngineCore startup failed; stopping the attempt");
                }
            }
            // A failed teardown must not start a temporary retry beside the old engine.
            if let Err(reason) = engine.shutdown(config.launch.drain_timeout()).await {
                return Err(EngineStartupFailure::Other(io::Error::other(format!(
                    "{}; engine cleanup also failed: {reason}",
                    error.into_error()
                ))));
            }
            Err(error)
        }
    }
}

/// Read effective connector settings after cache layout is finalized, before publishing readiness.
async fn observe_kv_transfer(
    client: &EngineCoreClient,
    configured: Option<RuntimeKvTransferMetadata>,
    startup_deadline: Instant,
) -> Result<Option<RuntimeKvTransferMetadata>, EngineStartupFailure> {
    let Some(configured) = configured else {
        return Ok(None);
    };
    if !configured.bidirectional_kv_xfer {
        return Ok(Some(configured));
    }
    let values = tokio::time::timeout_at(
        startup_deadline.into(),
        client.call_utility::<serde_json::Value, _>(
            "kv_connector_utility",
            ("configuration", serde_json::json!({})),
        ),
    )
    .await
    .map_err(|_| {
        EngineStartupFailure::Other(io::Error::other(
            "KV configuration observation exceeded startup deadline",
        ))
    })?
    .map_err(|error| EngineStartupFailure::Other(io::Error::other(error.to_string())))?;
    let mut settings = values
        .into_iter()
        .map(serde_json::from_value::<RuntimeKvTransferMetadata>);
    let first = settings
        .next()
        .ok_or_else(|| {
            EngineStartupFailure::Other(io::Error::other(
                "no KV connector configuration was reported",
            ))
        })?
        .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
    for setting in settings {
        let setting =
            setting.map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
        if setting != first {
            return Err(EngineStartupFailure::Other(io::Error::other(
                "engines reported different KV connector configurations",
            )));
        }
    }
    if first.connector != configured.connector {
        return Err(EngineStartupFailure::Other(io::Error::other(
            "engine KV connector differs from its launch configuration",
        )));
    }
    Ok(Some(first))
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

/// Report a mounted cache's write failure to a worker supervisor, or remain pending without a cache.
pub async fn wait_cache_write_failure(
    cache: Option<&runtime_cache::Config>,
    mode: runtime_cache::Mode,
) -> io::Error {
    let Some(cache) = cache else {
        return std::future::pending().await;
    };
    cache.wait_until_unwritable(mode).await
}

/// Inspect installed packages and optional CUDA registration without importing engine plugins.
async fn inspect_engine_environment(
    python: &str,
    environment: &[(String, String)],
    mooncake_rdma: bool,
) -> Result<VllmInspection, Box<dyn std::error::Error>> {
    let output = tokio::process::Command::new(python)
        .kill_on_drop(true)
        .envs(environment.iter().cloned())
        .args([
            "-c",
            r#"import json
import sys
from importlib.metadata import version, PackageNotFoundError

vllm_version = version("vllm")
try:
    version("modelexpress")
except PackageNotFoundError:
    model_express = False
else:
    model_express = True
dma_buf = False
if sys.argv[1] == "1":
    from connectors.mooncake.environment import prefer_dma_buf
    dma_buf = prefer_dma_buf()
print(json.dumps([vllm_version, model_express, dma_buf]))
"#,
        ])
        .arg(if mooncake_rdma { "1" } else { "0" })
        .output()
        .await?;
    if !output.status.success() {
        return Err(format!(
            "could not inspect engine environment using {python}: {}",
            String::from_utf8_lossy(&output.stderr).trim(),
        )
        .into());
    }
    let (version, model_express_available, mooncake_dma_buf): (String, bool, bool) =
        serde_json::from_slice(&output.stdout)?;
    let mut parts = version.split('.');
    let major = parts.next().and_then(|part| part.parse::<u64>().ok());
    let minor = parts.next().and_then(|part| part.parse::<u64>().ok());
    match (major, minor) {
        (Some(0), Some(minor @ 20..=31)) => {
            let protocol = match minor {
                20 => EngineCoreProtocol::V0_20,
                21..=25 => EngineCoreProtocol::V0_21ToV0_25,
                26..=27 => EngineCoreProtocol::V0_26ToV0_27,
                _ => EngineCoreProtocol::V0_28ToV0_31,
            };
            // Match the native lifecycle profiles installed by the vLLM patch catalog.
            let retained_prefixes = minor == 31 || version.split('+').next() == Some("0.30.0.dev0");
            Ok(VllmInspection {
                protocol,
                retained_prefixes,
                model_express_available,
                mooncake_dma_buf,
            })
        }
        _ => Err(format!(
            "unsupported vLLM version `{version}`; supported versions are 0.20 through 0.31"
        )
        .into()),
    }
}
