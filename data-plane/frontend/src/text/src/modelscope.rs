// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Downloads frontend model metadata through the ModelScope Hub HTTP contract.

use std::collections::BTreeSet;
use std::path::{Component, Path, PathBuf};

use serde::Deserialize;
use thiserror::Error;

const MODELSCOPE_ENDPOINT: &str = "https://www.modelscope.cn";
const MODELSCOPE_CACHE_ENV: &str = "MODELSCOPE_CACHE";
const TEMPORARY_MODELSCOPE_CACHE_DIR_ENV: &str = "FORETOKEN_TEMPORARY_MODELSCOPE_CACHE_DIR";

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct ApiResponse<T> {
    code: i64,
    message: String,
    data: Option<T>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RepositoryFiles {
    files: Option<Vec<RepositoryFile>>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RepositoryFile {
    path: String,
    #[serde(rename = "Type")]
    kind: String,
}

/// Returns a cached ModelScope directory or downloads the files needed by the frontend runtime.
pub async fn resolve_snapshot(
    model_id: &str,
    revision: &str,
    accepted_files: &[&str],
) -> Result<PathBuf, ModelScopeError> {
    let persistent = model_cache_root()?;
    let persistent_snapshot = model_directory(&persistent, model_id)?;
    if snapshot_has_frontend_artifact(&persistent_snapshot, accepted_files) {
        return Ok(persistent_snapshot);
    }
    if std::env::var("HF_HUB_OFFLINE").is_ok_and(|value| value == "1") {
        return Err(ModelScopeError::OfflineCacheMiss);
    }

    let download_root = std::env::var_os(TEMPORARY_MODELSCOPE_CACHE_DIR_ENV)
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
        .unwrap_or(persistent);
    let snapshot = model_directory(&download_root, model_id)?;
    let client = reqwest::Client::builder()
        .user_agent(concat!("foretoken/", env!("CARGO_PKG_VERSION")))
        .build()?;
    let files = repository_files(&client, model_id, revision).await?;
    let files = files
        .into_iter()
        .filter(|file| file.kind == "blob")
        .map(|file| file.path)
        .filter(|name| {
            accepted_files.contains(&name.as_str())
                || name.ends_with(".tiktoken")
                || name.ends_with(".jinja")
        })
        .collect::<BTreeSet<_>>();
    if files.is_empty() {
        return Err(ModelScopeError::NoFrontendArtifact);
    }
    for file in files {
        download_file(&client, model_id, revision, &file, &snapshot).await?;
    }
    Ok(snapshot)
}

fn snapshot_has_frontend_artifact(snapshot: &Path, accepted_files: &[&str]) -> bool {
    accepted_files
        .iter()
        .any(|file| snapshot.join(file).is_file())
}

fn model_cache_root() -> Result<PathBuf, ModelScopeError> {
    std::env::var_os(MODELSCOPE_CACHE_ENV)
        .map(PathBuf::from)
        .ok_or(ModelScopeError::MissingCacheRoot)
}

fn model_directory(root: &Path, model_id: &str) -> Result<PathBuf, ModelScopeError> {
    let identifier = Path::new(model_id);
    if identifier.is_absolute()
        || identifier
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(ModelScopeError::InvalidModelId);
    }
    Ok(root.join("models").join(identifier))
}

async fn repository_files(
    client: &reqwest::Client,
    model_id: &str,
    revision: &str,
) -> Result<Vec<RepositoryFile>, ModelScopeError> {
    let mut url = model_url(model_id, "repo/files")?;
    url.query_pairs_mut()
        .append_pair("Revision", revision)
        .append_pair("Recursive", "true");
    let response = client.get(url).send().await?;
    let status = response.status();
    let response: ApiResponse<RepositoryFiles> = response.json().await?;
    if !status.is_success() || response.code != 200 {
        return Err(ModelScopeError::Api(response.message));
    }
    Ok(response
        .data
        .ok_or_else(|| ModelScopeError::Api("missing repository file data".into()))?
        .files
        .unwrap_or_default())
}

async fn download_file(
    client: &reqwest::Client,
    model_id: &str,
    revision: &str,
    file: &str,
    snapshot: &Path,
) -> Result<(), ModelScopeError> {
    let relative = Path::new(file);
    if relative.is_absolute()
        || relative
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(ModelScopeError::InvalidRepositoryPath(file.into()));
    }
    let destination = snapshot.join(relative);
    if destination.is_file() {
        return Ok(());
    }
    let mut url = model_url(model_id, "repo")?;
    url.query_pairs_mut()
        .append_pair("Revision", revision)
        .append_pair("FilePath", file);
    let response = client.get(url).send().await?;
    if !response.status().is_success() {
        return Err(ModelScopeError::Download {
            file: file.into(),
            status: response.status().as_u16(),
        });
    }
    let bytes = response.bytes().await?;
    let parent = destination
        .parent()
        .ok_or_else(|| ModelScopeError::InvalidRepositoryPath(file.into()))?;
    std::fs::create_dir_all(parent)?;
    let temporary = destination.with_extension(format!(
        "{}.foretoken-part-{}",
        destination
            .extension()
            .and_then(|value| value.to_str())
            .unwrap_or("download"),
        std::process::id()
    ));
    std::fs::write(&temporary, bytes)?;
    std::fs::rename(&temporary, &destination)?;
    Ok(())
}

fn model_url(model_id: &str, suffix: &str) -> Result<reqwest::Url, ModelScopeError> {
    let mut url = reqwest::Url::parse(MODELSCOPE_ENDPOINT)
        .map_err(|error| ModelScopeError::Api(error.to_string()))?;
    let mut segments = url
        .path_segments_mut()
        .map_err(|_| ModelScopeError::InvalidModelId)?;
    segments.extend(["api", "v1", "models"]);
    for segment in model_id.split('/') {
        if segment.is_empty() || segment == "." || segment == ".." {
            return Err(ModelScopeError::InvalidModelId);
        }
        segments.push(segment);
    }
    segments.extend(suffix.split('/'));
    drop(segments);
    Ok(url)
}

#[derive(Debug, Error)]
pub enum ModelScopeError {
    #[error("MODELSCOPE_CACHE must be set for ModelScope loading")]
    MissingCacheRoot,
    #[error("ModelScope model identifier must be a relative repository path")]
    InvalidModelId,
    #[error("ModelScope repository returned an invalid path {0:?}")]
    InvalidRepositoryPath(String),
    #[error("ModelScope snapshot is not available in the offline cache")]
    OfflineCacheMiss,
    #[error("ModelScope repository has no supported frontend artifact")]
    NoFrontendArtifact,
    #[error("ModelScope API failed: {0}")]
    Api(String),
    #[error("could not download ModelScope artifact {file}: HTTP {status}")]
    Download { file: String, status: u16 },
    #[error("ModelScope HTTP request failed: {0}")]
    Http(#[from] reqwest::Error),
    #[error("ModelScope cache operation failed: {0}")]
    Io(#[from] std::io::Error),
}
