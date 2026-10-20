// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Starts one engine attempt, owns its connection deadline, and classifies cache retry failures.

use std::io;
use std::path::PathBuf;
use std::time::Instant;

use crate::backend::vllm::config::RuntimeConfig;
use crate::backend::vllm::prefix_lookup;
use crate::backend::vllm::transport::LOOPBACK_HOST;
use crate::engine::{ManagedEngine, PYTHON_MODULE_PATH};
use crate::model_cache::preparation;
use crate::{profiling, runtime_cache};
use foretoken_artifacts::ModelSource;
use foretoken_model_protocol::PreparedTokenizer;
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
}

/// A spawned member before the leader establishes request transport.
pub struct SpawnedEngine {
    pub engine: ManagedEngine,
    protocol: EngineCoreProtocol,
    handshake_port: u16,
    prepared_tokenizer: Option<PreparedTokenizer>,
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
    let (connected, _, _) =
        start_with_cache_fallback(config, startup_deadline, async |mode, deadline| {
            start_engine_attempt(config, cache, profiling, mode, deadline, cache_server).await
        })
        .await?;
    Ok(connected)
}

/// Retries one failed persistent-cache startup with Pod-scoped temporary storage.
/// Returns the original deadline for startup metadata; both attempts share it and must stop failed children.
pub async fn start_with_cache_fallback<T>(
    config: &RuntimeConfig,
    deadline: Instant,
    mut attempt: impl AsyncFnMut(runtime_cache::Mode, Instant) -> Result<T, EngineStartupFailure>,
) -> io::Result<(T, runtime_cache::Mode, Instant)> {
    match attempt(runtime_cache::Mode::Persistent, deadline).await {
        Ok(started) => Ok((started, runtime_cache::Mode::Persistent, deadline)),
        Err(EngineStartupFailure::PersistentCache { context, source }) => {
            // Distributed members restart together; a local retry would reuse stale peers.
            if config.member.is_some() {
                return Err(io::Error::new(
                    source.kind(),
                    format!("{context}: {source}"),
                ));
            }
            warn!(
                cache_mode = runtime_cache::Mode::Temporary.as_str(), error = %source, %context,
                "persistent RuntimeCache became unavailable; retrying with Pod-scoped temporary storage"
            );
            let started = attempt(runtime_cache::Mode::Temporary, deadline)
                .await
                .map_err(|failure| {
                    io::Error::other(format!(
                        "temporary RuntimeCache retry failed: {}",
                        failure.into_error()
                    ))
                })?;
            info!(
                cache_mode = runtime_cache::Mode::Temporary.as_str(),
                "engine started with Pod-scoped temporary cache storage"
            );
            Ok((started, runtime_cache::Mode::Temporary, deadline))
        }
        Err(failure) => Err(failure.into_error()),
    }
}

/// Materialized inputs shared by native EngineCore and the DT role supervisor.
pub struct PreparedEngineLaunch {
    pub model: String,
    pub arguments: Vec<String>,
    pub environment: Vec<(String, String)>,
    pub prepared_tokenizer: Option<PreparedTokenizer>,
    pub resolved_revision: Option<String>,
    pub resolved_draft_revision: Option<String>,
}

/// Prepares one launch without starting a process; the native and DT owners share its artifacts and cache mode.
pub async fn prepare_engine_launch(
    config: &RuntimeConfig,
    cache: Option<&runtime_cache::Config>,
    profiling: Option<&profiling::Config>,
    mode: runtime_cache::Mode,
    startup_deadline: Instant,
) -> Result<PreparedEngineLaunch, EngineStartupFailure> {
    let mut environment = prepare_environment(config, cache, profiling, mode)?;
    let mut managed_engine = config
        .launch
        .managed_engine(
            config.ports.handshake,
            config.member.as_ref(),
            &config.ports,
        )
        .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
    if let Some(member) = &config.member {
        environment.push(("VLLM_HOST_IP".into(), member.address.to_string()));
    }
    let (prepared_tokenizer, resolved_revision, resolved_draft_revision) = prepare_model_artifacts(
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
        }
    }
    Ok(PreparedEngineLaunch {
        model: managed_engine.model,
        arguments: managed_engine.python_args,
        environment,
        prepared_tokenizer,
        resolved_revision,
        resolved_draft_revision,
    })
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
    let PreparedEngineLaunch {
        model,
        arguments,
        environment,
        prepared_tokenizer,
        ..
    } = prepare_engine_launch(config, cache, profiling, mode, startup_deadline).await?;
    let handshake_port = config.ports.handshake;
    let mut managed_engine = config
        .launch
        .managed_engine(handshake_port, config.member.as_ref(), &config.ports)
        .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
    managed_engine.model = model;
    managed_engine.python_args = arguments;
    // Inspect the actual interpreter once before selecting wire and optional-loader behavior.
    let protocol_timeout = startup_deadline.saturating_duration_since(Instant::now());
    if protocol_timeout.is_zero() {
        return Err(EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed before package inspection",
        )));
    }
    let (engine_protocol, model_express_available) = tokio::time::timeout(
        protocol_timeout,
        inspect_engine_packages(&managed_engine.python, &environment),
    )
    .await
    .map_err(|_| {
        EngineStartupFailure::Other(io::Error::other(
            "EngineCore startup deadline elapsed during package inspection",
        ))
    })?
    .map_err(|error| classify_engine_startup_failure(cache, mode, format!("{error}")))?;
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
    if config.launch.dt.is_some()
        || config.member.is_some()
        || config.launch.shared_prefix_lookup()
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
) -> Result<(Option<PreparedTokenizer>, Option<String>, Option<String>), EngineStartupFailure> {
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
    let mut prepared_draft = None;
    let mut resolved_revision = None;
    let mut resolved_draft_revision = None;
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
                config
                    .launch
                    .dt
                    .as_ref()
                    .and_then(|dt| dt.draft_artifacts.as_ref()),
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
        resolved_revision = Some(prepared.model_revision().to_owned());
        resolved_draft_revision = prepared
            .draft
            .as_ref()
            .map(|draft| draft.model_revision().to_owned());
        prepared_draft = prepared
            .draft
            .as_ref()
            .map(|draft| draft.model.display().to_string());
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
    if let Some(draft) = config
        .launch
        .dt
        .as_ref()
        .and_then(|dt| dt.draft_artifacts.as_ref())
    {
        if draft.source == ModelSource::Local {
            prepared_draft = Some(
                preparation::local_artifact_path(&draft.model)
                    .map_err(EngineStartupFailure::Other)?,
            );
        }
        if let Some(model) = prepared_draft {
            // The published Draft snapshot follows the same source/cache lifecycle as Target.
            // Its native config must consume that snapshot instead of reacquiring weights.
            for argument in &mut managed_engine.python_args {
                if let Some(value) = argument.strip_prefix("--speculative-config=") {
                    let mut speculative: serde_json::Value = serde_json::from_str(value)
                        .map_err(|error| EngineStartupFailure::Other(io::Error::other(error)))?;
                    let object = speculative.as_object_mut().ok_or_else(|| {
                        EngineStartupFailure::Other(io::Error::other(
                            "speculative-config must be an object",
                        ))
                    })?;
                    object.insert("model".into(), serde_json::Value::String(model.clone()));
                    object.remove("revision");
                    *argument = format!("--speculative-config={speculative}");
                }
            }
        }
    }
    Ok((
        prepared_tokenizer,
        resolved_revision,
        resolved_draft_revision,
    ))
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
    // Upstream handshake waits are per phase; this owner bounds the complete connection.
    let client = tokio::select! {
        result = EngineCoreClient::connect_with_protocol(client_config, protocol) => match result {
            Ok(client) => Ok(client),
            Err(error) => Err(classify_engine_startup_failure(cache, mode, format!("could not connect to EngineCore: {error}"))),
        },
        status = engine.wait_for_exit() => Err(classify_engine_startup_failure(cache, mode, format!("managed EngineCore exited during startup: {status}"))),
        _ = tokio::time::sleep_until(startup_deadline.into()) => Err(EngineStartupFailure::Other(io::Error::other("EngineCore connection exceeded the startup deadline"))),
        reason = runtime_cache::wait_for_server(cache_server) => Err(EngineStartupFailure::Other(io::Error::other(format!("cache observation server stopped during startup: {reason}")))),
        error = wait_cache_write_failure(cache, mode) => Err(cache_mode_failure(mode, format!("{} cache became unwritable during EngineCore startup", mode.as_str()), error)),
    };
    match client {
        Ok(client) => Ok(ConnectedEngine {
            engine,
            client,
            cache_mode: mode,
            prepared_tokenizer,
        }),
        Err(error) => {
            // A failed teardown must not start a temporary retry beside the old engine.
            engine
                .shutdown(config.launch.drain_timeout())
                .await
                .map_err(|reason| EngineStartupFailure::Other(io::Error::other(reason)))?;
            Err(error)
        }
    }
}

/// Classifies one cache operation for the native or DT startup owner; only persistent failures permit retry.
pub fn cache_mode_failure(
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

/// Preserves an engine failure unless the startup owner proves its current cache has become unwritable.
pub fn classify_engine_startup_failure(
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

/// Inspect wire protocol and optional weight-loader support without importing engine plugins.
async fn inspect_engine_packages(
    python: &str,
    environment: &[(String, String)],
) -> Result<(EngineCoreProtocol, bool), Box<dyn std::error::Error>> {
    let output = tokio::process::Command::new(python)
        .kill_on_drop(true)
        .envs(environment.iter().cloned())
        .args([
            "-c",
            r#"import json
from importlib.metadata import version, PackageNotFoundError

vllm_version = version("vllm")
try:
    version("modelexpress")
except PackageNotFoundError:
    model_express = False
else:
    model_express = True
print(json.dumps([vllm_version, model_express]))
"#,
        ])
        .output()
        .await?;
    if !output.status.success() {
        return Err(format!("could not inspect engine packages using {python}").into());
    }
    let (version, model_express_available): (String, bool) =
        serde_json::from_slice(&output.stdout)?;
    let mut parts = version.split('.');
    let major = parts.next().and_then(|part| part.parse::<u64>().ok());
    let minor = parts.next().and_then(|part| part.parse::<u64>().ok());
    let protocol = match (major, minor) {
        (Some(0), Some(20)) => EngineCoreProtocol::V0_20,
        (Some(0), Some(21..=25)) => EngineCoreProtocol::V0_21ToV0_25,
        (Some(0), Some(26..=27)) => EngineCoreProtocol::V0_26ToV0_27,
        (Some(0), Some(28..=31)) => EngineCoreProtocol::V0_28ToV0_31,
        _ => {
            return Err(format!(
                "unsupported vLLM version `{version}`; supported versions are 0.20 through 0.31"
            )
            .into());
        }
    };
    Ok((protocol, model_express_available))
}
