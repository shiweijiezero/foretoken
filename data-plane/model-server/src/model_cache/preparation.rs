// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Publishes fixed source revisions and materializes their provider-owned snapshots.

use std::fs::{self, File, OpenOptions, TryLockError};
use std::io::{self, Read, Seek, Write};
use std::os::unix::fs::PermissionsExt;
use std::path::{Path, PathBuf};
use std::process::Stdio;

use foretoken_artifacts::{ModelArtifacts, ModelSource};
use foretoken_model_protocol::PreparedTokenizer;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::net::TcpListener;
use tokio::process::Command;
use tokio_util::sync::CancellationToken;

use crate::engine::PYTHON_MODULE_PATH;
use crate::model_cache::kubernetes::Kubernetes;
use crate::runtime_cache;

/// Controller-owned publication scope shared by replicas of one Pool revision.
pub const PREPARATION_SCOPE_ENV: &str = "FORETOKEN_MODEL_PREPARATION_SCOPE";
const SOURCE_RECORD_ENV: &str = "FORETOKEN_MODEL_PREPARATION_RECORD";
const SOURCE_CONFIG_MAP_ENV: &str = "FORETOKEN_MODEL_PREPARATION_CONFIGMAP";
const SOURCE_NAMESPACE_ENV: &str = "FORETOKEN_MODEL_PREPARATION_NAMESPACE";
const SOURCE_KEY: &str = "source.json";
const CACHE_LOCK: &str = ".foretoken-cache.lock";
pub(crate) const CACHE_ROOT_KEY: &str = "cache-root";

/// Cache locations and child overrides selected by the model-server's storage lifetime.
/// Published provider files may be shared; acquisition state belongs to the serving namespace.
pub struct PreparationStorage<'a> {
    pub model_root: &'a Path,
    pub state_root: &'a Path,
    /// Persistent model and index roots available during a temporary-cache retry.
    pub reusable_roots: Option<(&'a Path, &'a Path)>,
    pub environment: &'a [(String, String)],
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct ResolvedArtifacts {
    version: u8,
    pub(crate) artifacts: ModelArtifacts,
    pub(crate) model_revision: String,
    pub(crate) tokenizer_revision: String,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct ResolvedRevisions {
    model_revision: String,
    tokenizer_revision: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreparedArtifacts {
    source: ResolvedArtifacts,
    pub model: PathBuf,
    pub tokenizer: PathBuf,
    model_files: Vec<String>,
    tokenizer_files: Vec<String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct DownloadedArtifacts {
    model: PathBuf,
    tokenizer: PathBuf,
    model_files: Vec<String>,
    tokenizer_files: Vec<String>,
}

/// Resolves a local model or tokenizer to the UTF-8 path consumed by native engine startup.
pub fn local_artifact_path(identifier: &str) -> io::Result<String> {
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

/// Freezes source revisions for a controller-owned resolve-only Job without downloading weights.
pub async fn resolve(artifacts: &ModelArtifacts, python: &str) -> io::Result<()> {
    let name = std::env::var(SOURCE_CONFIG_MAP_ENV).map_err(io::Error::other)?;
    let model_root = foretoken_artifacts::model_cache_root().unwrap_or_else(std::env::temp_dir);
    let state_root =
        foretoken_artifacts::preparation_root().unwrap_or_else(|| model_root.join("prepared"));
    let storage = PreparationStorage {
        model_root: &model_root,
        state_root: &state_root,
        reusable_roots: None,
        environment: &[],
    };
    publish_source(artifacts, python, &storage, &name, None).await?;
    Ok(())
}

/// Runs a controller-owned download Job, including cache observation and signal shutdown.
/// Both inference-engine entrypoints use this lifecycle before allocating accelerator workloads.
pub async fn prepare_job(artifacts: &ModelArtifacts, python: &str) -> io::Result<()> {
    let model_root = foretoken_artifacts::model_cache_root().ok_or_else(|| {
        io::Error::other("FORETOKEN_MODEL_CACHE_ROOT must be set for model preparation")
    })?;
    let state_root = foretoken_artifacts::preparation_root().ok_or_else(|| {
        io::Error::other("FORETOKEN_MODEL_PREPARATION_ROOT must be set for model preparation")
    })?;
    let storage = PreparationStorage {
        model_root: &model_root,
        state_root: &state_root,
        reusable_roots: None,
        environment: &[],
    };
    let scope = std::env::var(PREPARATION_SCOPE_ENV).map_err(io::Error::other)?;
    let shutdown = CancellationToken::new();
    let mut observer =
        if let Some(cache) = runtime_cache::Config::from_env().map_err(io::Error::other)? {
            let listener = TcpListener::bind(("0.0.0.0", cache.observation_port())).await?;
            Some(tokio::spawn(runtime_cache::serve(
                listener,
                cache,
                shutdown.clone(),
            )))
        } else {
            None
        };
    let result = tokio::select! {
        result = prepare(artifacts, python, &storage, &scope, false) => result.map(|_| ()),
        reason = runtime_cache::wait_for_server(&mut observer) => Err(io::Error::other(reason)),
        signal = crate::shutdown_signal() => signal.and_then(|()| {
            Err(io::Error::new(io::ErrorKind::Interrupted, "model preparation cancelled"))
        }),
    };
    shutdown.cancel();
    if let Some(observer) = observer {
        observer.await.map_err(io::Error::other)??;
    }
    result
}

/// Publishes fixed provider versions and materializes files under a namespace-private acquisition lock.
/// Serving processes consume the mounted source record; paths remain a disposable index.
/// `require_publication` prevents a restart from choosing a new moving source revision.
pub async fn prepare(
    artifacts: &ModelArtifacts,
    python: &str,
    storage: &PreparationStorage<'_>,
    scope: &str,
    require_publication: bool,
) -> io::Result<PreparedArtifacts> {
    // Registration precedes cache use under the same lock that excludes collection.
    // Serving processes remain protected by their durable source record after this stage returns.
    let cache_lock = lock_model_cache(storage.model_root, false).await?;
    let identity = model_cache_identity(&cache_lock)?;
    let directory = foretoken_artifacts::preparation_directory(storage.state_root, scope)?;
    let mut published_cache = None;
    let source = if let Ok(name) = std::env::var(SOURCE_CONFIG_MAP_ENV) {
        published_cache = Some(identity.clone());
        Some(publish_source(artifacts, python, storage, &name, Some(&identity)).await?)
    } else if let Some(path) = std::env::var_os(SOURCE_RECORD_ENV) {
        let path = Path::new(&path);
        published_cache = match fs::read_to_string(path.with_file_name(CACHE_ROOT_KEY)) {
            Ok(value) => Some(value),
            Err(error) if error.kind() == io::ErrorKind::NotFound => None,
            Err(error) => return Err(error),
        };
        if storage.reusable_roots.is_none()
            && published_cache
                .as_ref()
                .is_some_and(|expected| *expected != identity)
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "mounted model source publication belongs to another cache directory",
            ));
        }
        Some(read_source(path, artifacts)?.ok_or_else(|| {
            io::Error::new(
                io::ErrorKind::NotFound,
                "mounted model source publication is missing",
            )
        })?)
    } else {
        read_source(&directory.join(SOURCE_KEY), artifacts)?
    };
    if let Some(source) = &source {
        // Compile-cache write failure does not invalidate readable model snapshots.
        // An inaccessible persistent path index is optional; acquisition uses the fixed publication.
        if let Some((model_root, state_root)) = storage.reusable_roots {
            let same_cache = published_cache.as_ref().is_none_or(|expected| {
                rustix::fs::open(
                    model_root.join(CACHE_LOCK),
                    rustix::fs::OFlags::RDONLY | rustix::fs::OFlags::NOFOLLOW,
                    rustix::fs::Mode::empty(),
                )
                .map(File::from)
                .map_err(io::Error::from)
                .and_then(|file| model_cache_identity(&file))
                .is_ok_and(|actual| actual == *expected)
            });
            let reusable = foretoken_artifacts::preparation_directory(state_root, scope)?;
            if same_cache && let Ok(Some(result)) = load_result(&reusable, source) {
                return Ok(result);
            }
        }
        if let Some(result) = load_result(&directory, source)? {
            return Ok(result);
        }
    } else if require_publication {
        return Err(io::Error::new(
            io::ErrorKind::NotFound,
            "published model source revisions are missing",
        ));
    }
    fs::create_dir_all(&directory)?;
    let lock = OpenOptions::new()
        .create(true)
        .truncate(false)
        .read(true)
        .write(true)
        .open(directory.join("prepare.lock"))?;
    let _lock = tokio::task::spawn_blocking(move || {
        lock.lock()?;
        Ok::<_, io::Error>(lock)
    })
    .await
    .map_err(io::Error::other)??;
    let source = match source {
        Some(source) => source,
        None => match read_source(&directory.join(SOURCE_KEY), artifacts)? {
            Some(source) => source,
            None => {
                let source = resolve_revisions(artifacts, python, storage).await?;
                publish_json(&directory, SOURCE_KEY, &source)?;
                source
            }
        },
    };
    if let Some(result) = load_result(&directory, &source)? {
        return Ok(result);
    }
    let mut command = provider_command(artifacts, python, storage)?;
    command
        .env("FORETOKEN_PREPARE_ACTION", "download")
        .env("FORETOKEN_PREPARE_MODEL_REVISION", &source.model_revision)
        .env(
            "FORETOKEN_PREPARE_TOKENIZER_REVISION",
            &source.tokenizer_revision,
        );
    let downloaded: DownloadedArtifacts = run_provider(&mut command).await?;
    let result = PreparedArtifacts {
        source,
        model: downloaded.model,
        tokenizer: downloaded.tokenizer,
        model_files: downloaded.model_files,
        tokenizer_files: downloaded.tokenizer_files,
    };
    publish_json(&directory, "result.json", &result)?;
    Ok(result)
}

/// Coordinates source publication and provider writes with shared-cache collection.
/// The lock inode outlives individual entries; cancellable acquisition does not strand blocking threads.
pub(crate) async fn lock_model_cache(root: &Path, exclusive: bool) -> io::Result<File> {
    fs::create_dir_all(root)?;
    let descriptor = rustix::fs::open(
        root.join(CACHE_LOCK),
        rustix::fs::OFlags::CREATE | rustix::fs::OFlags::RDWR | rustix::fs::OFlags::NOFOLLOW,
        rustix::fs::Mode::RUSR
            | rustix::fs::Mode::WUSR
            | rustix::fs::Mode::RGRP
            | rustix::fs::Mode::WGRP,
    )?;
    let mut file = File::from(descriptor);
    acquire_cache_lock(&file, exclusive).await?;
    if file.metadata()?.len() == 0 {
        if !exclusive {
            file.unlock()?;
            acquire_cache_lock(&file, true).await?;
        }
        // A mount alias does not identify a dataset. Retain its identity in the same
        // inode used for coordination so NFS exports and namespace claims agree.
        if file.metadata()?.len() == 0 {
            file.write_all(uuid::Uuid::new_v4().as_bytes())?;
            file.sync_all()?;
        }
        if !exclusive {
            acquire_cache_lock(&file, false).await?;
        }
    }
    Ok(file)
}

async fn acquire_cache_lock(file: &File, exclusive: bool) -> io::Result<()> {
    loop {
        let result = if exclusive {
            file.try_lock()
        } else {
            file.try_lock_shared()
        };
        match result {
            Ok(()) => return Ok(()),
            Err(TryLockError::WouldBlock) => {
                tokio::time::sleep(std::time::Duration::from_millis(100)).await
            }
            Err(TryLockError::Error(error)) => return Err(error),
        }
    }
}

/// Reads the storage identity while its caller holds the preparation or collection lock.
pub(crate) fn model_cache_identity(mut lock: &File) -> io::Result<String> {
    let mut identity = [0_u8; 16];
    lock.rewind()?;
    lock.read_exact(&mut identity)?;
    Ok(uuid::Uuid::from_bytes(identity).to_string())
}

impl PreparedArtifacts {
    /// Publishes the exact tokenizer source and files to frontend preparation.
    /// Only a persistent namespace/claim binding permits direct directory reuse.
    pub fn tokenizer_metadata(
        &self,
        scope: &str,
        cache_binding: Option<String>,
    ) -> PreparedTokenizer {
        PreparedTokenizer {
            scope: scope.into(),
            model: self.source.artifacts.tokenizer.clone(),
            revision: self.source.artifacts.tokenizer_revision.clone(),
            directory: self.tokenizer.display().to_string(),
            cache_binding,
            files: self
                .tokenizer_files
                .iter()
                .filter(|path| {
                    foretoken_artifacts::MODEL_METADATA_FILES.contains(&path.as_str())
                        || path.ends_with(".jinja")
                        || path.ends_with(".tiktoken")
                })
                .map(|path| (path.clone(), self.source.tokenizer_revision.clone()))
                .collect(),
        }
    }

    fn files_available(&self) -> bool {
        self.model_files
            .iter()
            .all(|file| self.model.join(file).is_file())
            && self
                .tokenizer_files
                .iter()
                .all(|file| self.tokenizer.join(file).is_file())
    }
}

/// Reads the fixed provider identities shared by acquisition and cache collection.
pub(crate) fn source_publication(bytes: &[u8]) -> io::Result<ResolvedArtifacts> {
    let source: ResolvedArtifacts = serde_json::from_slice(bytes).map_err(io::Error::other)?;
    if source.version != 1
        || source.model_revision.is_empty()
        || source.tokenizer_revision.is_empty()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "invalid model source publication",
        ));
    }
    Ok(source)
}

// The mounted record and ConfigMap carry source identity, never container-local paths.
fn decode_source(bytes: &[u8], artifacts: &ModelArtifacts) -> io::Result<ResolvedArtifacts> {
    let source = source_publication(bytes)?;
    if &source.artifacts != artifacts {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "model source publication does not match the selected artifacts",
        ));
    }
    Ok(source)
}

fn read_source(path: &Path, artifacts: &ModelArtifacts) -> io::Result<Option<ResolvedArtifacts>> {
    match fs::read(path) {
        Ok(bytes) => decode_source(&bytes, artifacts).map(Some),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn load_result(
    directory: &Path,
    source: &ResolvedArtifacts,
) -> io::Result<Option<PreparedArtifacts>> {
    let bytes = match fs::read(directory.join("result.json")) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    // Paths are a disposable index; only the separately read source publication is authoritative.
    let Ok(result) = serde_json::from_slice::<PreparedArtifacts>(&bytes) else {
        return Ok(None);
    };
    if &result.source != source {
        return Ok(None);
    }
    Ok(result.files_available().then_some(result))
}

// Provider imports happen in a child so transfer tuning does not affect the serving engine.
fn provider_command(
    artifacts: &ModelArtifacts,
    python: &str,
    storage: &PreparationStorage<'_>,
) -> io::Result<Command> {
    let model_root = storage.model_root;
    let (source, performance_setting, default) = match artifacts.source {
        ModelSource::Hf => ("hf", "HF_XET_HIGH_PERFORMANCE", "1"),
        ModelSource::ModelScope => ("modelscope", "MODELSCOPE_DOWNLOAD_PARALLELS", "16"),
        ModelSource::Local => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "local artifacts do not require source preparation",
            ));
        }
    };
    let directory = std::env::var_os(foretoken_artifacts::source::ACTIVE_DIRECTORY_ENV)
        .map(|directory| PathBuf::from(directory).join("python"))
        .unwrap_or_else(|| PathBuf::from(PYTHON_MODULE_PATH));
    let script = fs::read_to_string(directory.join("foretoken_prepare.py"))?;
    let mut command = Command::new(python);
    command
        .args(["-c", &script])
        .env(
            performance_setting,
            std::env::var_os(performance_setting).unwrap_or_else(|| default.into()),
        )
        .env("FORETOKEN_PREPARE_SOURCE", source)
        .env("FORETOKEN_PREPARE_MODEL", &artifacts.model)
        .env("FORETOKEN_PREPARE_TOKENIZER", &artifacts.tokenizer)
        .envs(
            storage
                .environment
                .iter()
                .map(|(name, value)| (name, value)),
        )
        .env(foretoken_artifacts::MODEL_CACHE_ROOT_ENV, model_root)
        .env("HF_HUB_CACHE", model_root.join("hub"))
        // Retained engine SDKs share the repository-local layout, not newer cache-wide blob stores.
        .env("HF_HUB_DISABLE_SHARED_BLOBS", "1")
        .env(
            foretoken_artifacts::MODELSCOPE_CACHE_ENV,
            foretoken_artifacts::modelscope_cache_root(model_root),
        )
        .stderr(Stdio::inherit())
        .kill_on_drop(true);
    Ok(command)
}

async fn run_provider<T: serde::de::DeserializeOwned>(command: &mut Command) -> io::Result<T> {
    let output = command
        .stdout(Stdio::piped())
        .spawn()?
        .wait_with_output()
        .await?;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "model source operation failed with status {}",
            output.status
        )));
    }
    serde_json::from_slice(&output.stdout).map_err(io::Error::other)
}

async fn resolve_revisions(
    artifacts: &ModelArtifacts,
    python: &str,
    storage: &PreparationStorage<'_>,
) -> io::Result<ResolvedArtifacts> {
    let mut command = provider_command(artifacts, python, storage)?;
    command
        .env("FORETOKEN_PREPARE_ACTION", "resolve")
        .env("FORETOKEN_PREPARE_MODEL_REVISION", &artifacts.revision)
        .env(
            "FORETOKEN_PREPARE_TOKENIZER_REVISION",
            &artifacts.tokenizer_revision,
        );
    let revisions: ResolvedRevisions = run_provider(&mut command).await?;
    let source = ResolvedArtifacts {
        version: 1,
        artifacts: artifacts.clone(),
        model_revision: revisions.model_revision,
        tokenizer_revision: revisions.tokenizer_revision,
    };
    decode_source(
        &serde_json::to_vec(&source).map_err(io::Error::other)?,
        artifacts,
    )
}

// ConfigMap resourceVersion makes the first successful publication authoritative across Jobs.
// A competing publisher adopts the winner instead of resolving or overwriting another version.
async fn publish_source(
    artifacts: &ModelArtifacts,
    python: &str,
    storage: &PreparationStorage<'_>,
    name: &str,
    cache_identity: Option<&str>,
) -> io::Result<ResolvedArtifacts> {
    let namespace = std::env::var(SOURCE_NAMESPACE_ENV).map_err(io::Error::other)?;
    let path = format!("/api/v1/namespaces/{namespace}/configmaps/{name}");
    let client = Kubernetes::in_cluster()?;
    let mut candidate: Option<ResolvedArtifacts> = None;
    loop {
        let current = client.get(&path).await?;
        if current["metadata"]["deletionTimestamp"].is_string() {
            return Err(io::Error::new(
                io::ErrorKind::Interrupted,
                "model source publication is retiring",
            ));
        }
        let recorded_root = current["data"][CACHE_ROOT_KEY].as_str();
        if let (Some(expected), Some(recorded)) = (cache_identity, recorded_root)
            && expected != recorded
        {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "model source publication belongs to another cache directory",
            ));
        }
        let source = if let Some(record) = current["data"][SOURCE_KEY]
            .as_str()
            .filter(|record| !record.is_empty())
        {
            let source = decode_source(record.as_bytes(), artifacts)?;
            if cache_identity.is_none() || cache_identity == recorded_root {
                return Ok(source);
            }
            source
        } else {
            match &candidate {
                Some(source) => source.clone(),
                None => candidate
                    .insert(resolve_revisions(artifacts, python, storage).await?)
                    .clone(),
            }
        };
        let resource_version =
            current["metadata"]["resourceVersion"]
                .as_str()
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "source ConfigMap has no resourceVersion",
                    )
                })?;
        let record = serde_json::to_string(&source).map_err(io::Error::other)?;
        let mut data = json!({(SOURCE_KEY): record});
        if let Some(identity) = cache_identity {
            data[CACHE_ROOT_KEY] = json!(identity);
        }
        match client
            .patch(
                &path,
                &json!({"metadata": {"resourceVersion": resource_version}, "data": data}),
            )
            .await
        {
            Ok(_) => return Ok(source),
            Err(kube::Error::Api(error)) if error.code == 409 => continue,
            Err(error) => return Err(io::Error::other(error)),
        }
    }
}

fn publish_json(directory: &Path, name: &str, value: &impl Serialize) -> io::Result<()> {
    // Keep the shared directory's group inheritance and the original umask-filtered file mode.
    let mut temporary = tempfile::Builder::new()
        .prefix(".publication-")
        .permissions(fs::Permissions::from_mode(0o666))
        .tempfile_in(directory)?;
    temporary.write_all(&serde_json::to_vec(value).map_err(io::Error::other)?)?;
    temporary
        .persist(directory.join(name))
        .map_err(|error| error.error)?;
    Ok(())
}
