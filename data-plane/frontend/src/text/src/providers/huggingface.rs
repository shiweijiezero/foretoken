// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Acquires frontend metadata through Hugging Face's native cache, credentials, and offline contract.

use std::collections::BTreeSet;
use std::path::{Path, PathBuf};

use foretoken_artifacts::MODEL_METADATA_FILES;
use foretoken_model_protocol::PreparedTokenizer;
use hf_hub::api::tokio::ApiBuilder;
use hf_hub::{Cache, Repo, RepoType, api::Siblings};

use crate::TextBackendLoadError;

/// Resolves the requested metadata snapshot for ordinary frontend processor loading.
pub(super) async fn resolve_snapshot(
    model_id: &str,
    revision: &str,
) -> Result<PathBuf, TextBackendLoadError> {
    if let Some(snapshot) = cached_model_snapshot(model_id, revision) {
        return Ok(snapshot);
    }
    if std::env::var(foretoken_artifacts::HF_HUB_OFFLINE_ENV).is_ok_and(|value| value == "1") {
        return Err(TextBackendLoadError::OfflineCacheMiss);
    }
    let api = api_builder()
        .build()
        .map_err(|_| TextBackendLoadError::HubClient)?;
    let repo = api.repo(Repo::with_revision(
        model_id.to_owned(),
        RepoType::Model,
        revision.to_owned(),
    ));
    let info = repo
        .info()
        .await
        .map_err(|_| TextBackendLoadError::RepositoryInfo)?;
    let mut snapshot = None;
    for file in metadata_files(&info.siblings) {
        let path = repo
            .get(&file)
            .await
            .map_err(|_| TextBackendLoadError::Download { file: file.clone() })?;
        snapshot = cache_snapshot_dir(&path);
    }
    snapshot.ok_or(TextBackendLoadError::NoTokenizerArtifact)
}

/// Fetches exactly the file revisions recorded by model acquisition, never a new moving reference.
pub(super) async fn resolve_prepared_snapshot(
    model_id: &str,
    prepared: &PreparedTokenizer,
) -> Result<PathBuf, TextBackendLoadError> {
    let offline =
        std::env::var(foretoken_artifacts::HF_HUB_OFFLINE_ENV).is_ok_and(|value| value == "1");
    let api = api_builder()
        .build()
        .map_err(|_| TextBackendLoadError::HubClient)?;
    let cache = foretoken_artifacts::temporary_model_root()
        .map(|root| root.join("hub"))
        .or_else(foretoken_artifacts::huggingface_cache_root)
        .map(Cache::new)
        .unwrap_or_else(Cache::from_env);
    let mut snapshot = None;
    for (file, revision) in &prepared.files {
        let repo = Repo::with_revision(model_id.into(), RepoType::Model, revision.clone());
        let path = match cache.repo(repo.clone()).get(file) {
            Some(path) => path,
            None if offline => return Err(TextBackendLoadError::OfflineCacheMiss),
            None => api
                .repo(repo)
                .get(file)
                .await
                .map_err(|_| TextBackendLoadError::Download { file: file.clone() })?,
        };
        snapshot = cache_snapshot_dir(&path);
    }
    snapshot.ok_or(TextBackendLoadError::NoTokenizerArtifact)
}

/// Applies the same credential and cache selection to both ordinary and prepared metadata reads.
fn api_builder() -> ApiBuilder {
    let mut builder = ApiBuilder::from_env().with_progress(false);
    if let Some(cache) = foretoken_artifacts::temporary_model_root()
        .map(|root| root.join("hub"))
        .or_else(foretoken_artifacts::huggingface_cache_root)
    {
        builder = builder.with_cache_dir(cache);
    }
    if let Ok(token) = std::env::var(foretoken_artifacts::HF_TOKEN_ENV)
        && !token.is_empty()
    {
        builder = builder.with_token(Some(token));
    }
    builder
}

fn cached_model_snapshot(model_id: &str, revision: &str) -> Option<PathBuf> {
    let cache = foretoken_artifacts::huggingface_cache_root()
        .map(Cache::new)
        .unwrap_or_else(Cache::from_env);
    let repo = cache.repo(Repo::with_revision(
        model_id.to_owned(),
        RepoType::Model,
        revision.to_owned(),
    ));
    MODEL_METADATA_FILES
        .iter()
        .find_map(|file| repo.get(file))?
        .parent()
        .map(Path::to_path_buf)
}

fn metadata_files(siblings: &[Siblings]) -> Vec<String> {
    siblings
        .iter()
        .map(|sibling| sibling.rfilename.as_str())
        .filter(|name| {
            MODEL_METADATA_FILES.contains(name)
                || name.ends_with(".tiktoken")
                || name.ends_with(".jinja")
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .map(str::to_owned)
        .collect()
}

fn cache_snapshot_dir(path: &Path) -> Option<PathBuf> {
    path.ancestors()
        .find(|directory| {
            directory
                .parent()
                .is_some_and(|parent| parent.ends_with("snapshots"))
        })
        .map(Path::to_path_buf)
}
