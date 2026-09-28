// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Publishes fixed source revisions and materializes their provider-owned snapshots.

use std::fs::{self, OpenOptions};
use std::io;
use std::path::{Path, PathBuf};
use std::process::Stdio;

use foretoken_artifacts::ModelSource;
use foretoken_model_protocol::PreparedTokenizer;
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::process::Command;

use crate::launch::{Artifacts, LaunchPlanV1};

/// Controller-owned publication scope shared by replicas of one Pool revision.
pub const PREPARATION_SCOPE_ENV: &str = "FORETOKEN_MODEL_PREPARATION_SCOPE";
const SOURCE_RECORD_ENV: &str = "FORETOKEN_MODEL_PREPARATION_RECORD";
const SOURCE_CONFIG_MAP_ENV: &str = "FORETOKEN_MODEL_PREPARATION_CONFIGMAP";
const SOURCE_NAMESPACE_ENV: &str = "FORETOKEN_MODEL_PREPARATION_NAMESPACE";
const SOURCE_KEY: &str = "source.json";
const SERVICE_ACCOUNT_DIRECTORY: &str = "/var/run/secrets/kubernetes.io/serviceaccount";

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct ResolvedArtifacts {
    version: u8,
    artifacts: Artifacts,
    model_revision: String,
    tokenizer_revision: String,
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

// The mounted record and ConfigMap carry source identity, never container-local paths.
fn decode_source(bytes: &[u8], artifacts: &Artifacts) -> io::Result<ResolvedArtifacts> {
    let source: ResolvedArtifacts = serde_json::from_slice(bytes).map_err(io::Error::other)?;
    if source.version != 1
        || &source.artifacts != artifacts
        || source.model_revision.is_empty()
        || source.tokenizer_revision.is_empty()
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "model source publication does not match the launch plan",
        ));
    }
    Ok(source)
}

fn read_source(path: &Path, artifacts: &Artifacts) -> io::Result<Option<ResolvedArtifacts>> {
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
fn provider_command(plan: &LaunchPlanV1, model_root: &Path) -> io::Result<Command> {
    let (source, performance_setting, default) = match plan.artifacts.source {
        ModelSource::Hf => ("hf", "HF_XET_HIGH_PERFORMANCE", "1"),
        ModelSource::ModelScope => ("modelscope", "MODELSCOPE_DOWNLOAD_PARALLELS", "16"),
        ModelSource::Local => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "local artifacts do not require source preparation",
            ));
        }
    };
    let mut command = Command::new(plan.python_executable());
    command
        .args(["-c", include_str!("../python/foretoken_prepare.py")])
        .env(
            performance_setting,
            std::env::var_os(performance_setting).unwrap_or_else(|| default.into()),
        )
        .env("FORETOKEN_PREPARE_SOURCE", source)
        .env("FORETOKEN_PREPARE_MODEL", &plan.artifacts.model)
        .env("FORETOKEN_PREPARE_TOKENIZER", &plan.artifacts.tokenizer)
        .env(foretoken_artifacts::MODEL_ROOT_ENV, model_root)
        .env("HF_HUB_CACHE", model_root.join("hub"))
        .env("HF_XET_CACHE", model_root.join("xet"))
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
    plan: &LaunchPlanV1,
    model_root: &Path,
) -> io::Result<ResolvedArtifacts> {
    let mut command = provider_command(plan, model_root)?;
    command
        .env("FORETOKEN_PREPARE_ACTION", "resolve")
        .env("FORETOKEN_PREPARE_MODEL_REVISION", &plan.artifacts.revision)
        .env(
            "FORETOKEN_PREPARE_TOKENIZER_REVISION",
            &plan.artifacts.tokenizer_revision,
        );
    let revisions: ResolvedRevisions = run_provider(&mut command).await?;
    let source = ResolvedArtifacts {
        version: 1,
        artifacts: plan.artifacts.clone(),
        model_revision: revisions.model_revision,
        tokenizer_revision: revisions.tokenizer_revision,
    };
    decode_source(
        &serde_json::to_vec(&source).map_err(io::Error::other)?,
        &plan.artifacts,
    )
}

// ConfigMap resourceVersion makes the first successful publication authoritative across Jobs.
// A competing publisher adopts the winner instead of resolving or overwriting another version.
async fn publish_source(
    plan: &LaunchPlanV1,
    model_root: &Path,
    name: &str,
) -> io::Result<ResolvedArtifacts> {
    let namespace = std::env::var(SOURCE_NAMESPACE_ENV).map_err(io::Error::other)?;
    let host = std::env::var("KUBERNETES_SERVICE_HOST").map_err(io::Error::other)?;
    let port = std::env::var("KUBERNETES_SERVICE_PORT_HTTPS").map_err(io::Error::other)?;
    let host = if host.contains(':') {
        format!("[{host}]")
    } else {
        host
    };
    let url = format!("https://{host}:{port}/api/v1/namespaces/{namespace}/configmaps/{name}");
    let account = Path::new(SERVICE_ACCOUNT_DIRECTORY);
    let client = reqwest::Client::builder()
        .no_proxy()
        .add_root_certificate(
            reqwest::Certificate::from_pem(&fs::read(account.join("ca.crt"))?)
                .map_err(io::Error::other)?,
        )
        .build()
        .map_err(io::Error::other)?;
    let mut candidate = None;
    loop {
        let token = fs::read_to_string(account.join("token"))?;
        let current: serde_json::Value = client
            .get(&url)
            .bearer_auth(token.trim())
            .send()
            .await
            .map_err(io::Error::other)?
            .error_for_status()
            .map_err(io::Error::other)?
            .json()
            .await
            .map_err(io::Error::other)?;
        if let Some(record) = current["data"][SOURCE_KEY]
            .as_str()
            .filter(|record| !record.is_empty())
        {
            return decode_source(record.as_bytes(), &plan.artifacts);
        }
        let resource_version =
            current["metadata"]["resourceVersion"]
                .as_str()
                .ok_or_else(|| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "source ConfigMap has no resourceVersion",
                    )
                })?;
        let source = match &candidate {
            Some(source) => source,
            None => candidate.insert(resolve_revisions(plan, model_root).await?),
        };
        let record = serde_json::to_string(source).map_err(io::Error::other)?;
        let response = client.patch(&url).bearer_auth(token.trim())
            .header(reqwest::header::CONTENT_TYPE, "application/merge-patch+json")
            .json(&json!({"metadata": {"resourceVersion": resource_version}, "data": {(SOURCE_KEY): record}}))
            .send().await.map_err(io::Error::other)?;
        if response.status() == reqwest::StatusCode::CONFLICT {
            continue;
        }
        response.error_for_status().map_err(io::Error::other)?;
        return Ok(source.clone());
    }
}

/// Freezes source revisions for a controller-owned resolve-only Job without downloading weights.
pub async fn resolve(plan: &LaunchPlanV1) -> io::Result<()> {
    let name = std::env::var(SOURCE_CONFIG_MAP_ENV).map_err(io::Error::other)?;
    let model_root = foretoken_artifacts::model_root().unwrap_or_else(std::env::temp_dir);
    publish_source(plan, &model_root, &name).await?;
    Ok(())
}

/// Publishes a source revision and materializes it under the Pool's cross-process lock.
/// Serving processes read the mounted publication; filesystem paths are only a reusable cache.
/// `require_publication` prevents a serving restart from choosing a new moving source revision.
pub async fn prepare(
    plan: &LaunchPlanV1,
    model_root: &Path,
    scope: &str,
    require_publication: bool,
    reusable_root: Option<&Path>,
) -> io::Result<PreparedArtifacts> {
    let directory = foretoken_artifacts::preparation_directory(model_root, scope)?;
    let source = if let Ok(name) = std::env::var(SOURCE_CONFIG_MAP_ENV) {
        Some(publish_source(plan, model_root, &name).await?)
    } else if let Some(path) = std::env::var_os(SOURCE_RECORD_ENV) {
        Some(
            read_source(Path::new(&path), &plan.artifacts)?.ok_or_else(|| {
                io::Error::new(
                    io::ErrorKind::NotFound,
                    "mounted model source publication is missing",
                )
            })?,
        )
    } else {
        read_source(&directory.join(SOURCE_KEY), &plan.artifacts)?
    };
    if let Some(source) = &source {
        // Compile-cache write failure does not invalidate readable model snapshots.
        // An inaccessible persistent path index is optional; acquisition uses the fixed publication.
        if let Some(root) = reusable_root {
            let reusable = foretoken_artifacts::preparation_directory(root, scope)?;
            if let Ok(Some(result)) = load_result(&reusable, source) {
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
        None => match read_source(&directory.join(SOURCE_KEY), &plan.artifacts)? {
            Some(source) => source,
            None => {
                let source = resolve_revisions(plan, model_root).await?;
                publish_json(&directory, SOURCE_KEY, &source)?;
                source
            }
        },
    };
    if let Some(result) = load_result(&directory, &source)? {
        return Ok(result);
    }
    let mut command = provider_command(plan, model_root)?;
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

fn publish_json(directory: &Path, name: &str, value: &impl Serialize) -> io::Result<()> {
    let temporary = directory.join(format!(".publication-{}", uuid::Uuid::new_v4()));
    fs::write(
        &temporary,
        serde_json::to_vec(value).map_err(io::Error::other)?,
    )?;
    fs::rename(temporary, directory.join(name))
}
