// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! ModelScope metadata adapter for the Rust-only frontend's local vLLM file resolver.

use std::path::{Component, Path, PathBuf};
use std::time::Duration;

use reqwest::{Client, Url};
use serde::Deserialize;
use tokio::io::AsyncWriteExt;

use crate::{TextBackendLoadError, is_model_file};

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct Listing {
    success: bool,
    data: Option<RepositoryFiles>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RepositoryFiles {
    files: Vec<RepositoryFile>,
}

#[derive(Deserialize)]
#[serde(rename_all = "PascalCase")]
struct RepositoryFile {
    path: String,
    revision: String,
    #[serde(rename = "Type")]
    kind: String,
}

// Publish only complete directories. The frontend may share a PVC with other replicas;
// an interrupted download must never become a cache hit on their next startup.
pub(super) async fn snapshot(
    model: &str,
    revision: &str,
    cache: &Path,
) -> Result<PathBuf, TextBackendLoadError> {
    if !safe_path(model) || !safe_path(revision) {
        return Err(TextBackendLoadError::ModelScopeListing);
    }
    let root = cache.join("modelscope-frontend").join(model).join("snapshots");
    let destination = root.join(revision);
    if destination.is_dir() {
        return Ok(destination);
    }
    if foretoken_model_source::offline() {
        return Err(TextBackendLoadError::OfflineCacheMiss);
    }
    let client = Client::builder().timeout(Duration::from_secs(120)).build()?;
    let mut url = repository_url(model)?;
    url.path_segments_mut().map_err(|_| TextBackendLoadError::ModelScopeListing)?.push("files");
    url.query_pairs_mut().append_pair("Revision", revision).append_pair("Recursive", "true");
    let listing: Listing = client.get(url).send().await?.error_for_status()?.json().await?;
    let files = listing.data.filter(|_| listing.success)
        .ok_or(TextBackendLoadError::ModelScopeListing)?.files;
    tokio::fs::create_dir_all(&root).await?;
    let staging = tempfile::Builder::new().prefix(".download-").tempdir_in(&root)?;
    let mut downloaded = false;
    for file in files.into_iter().filter(|file| file.kind == "blob" && is_model_file(&file.path)) {
        if !safe_path(&file.path) || file.revision.is_empty() {
            return Err(TextBackendLoadError::ModelScopeListing);
        }
        let mut url = repository_url(model)?;
        url.query_pairs_mut().append_pair("Revision", &file.revision).append_pair("FilePath", &file.path);
        let mut response = client.get(url).send().await?.error_for_status()?;
        let path = staging.path().join(&file.path);
        if let Some(parent) = path.parent() {
            tokio::fs::create_dir_all(parent).await?;
        }
        let mut output = tokio::fs::File::create(path).await?;
        while let Some(chunk) = response.chunk().await? {
            output.write_all(&chunk).await?;
        }
        output.flush().await?;
        downloaded = true;
    }
    if !downloaded {
        return Err(TextBackendLoadError::NoTokenizerArtifact);
    }
    if let Some(parent) = destination.parent() {
        tokio::fs::create_dir_all(parent).await?;
    }
    match tokio::fs::rename(staging.path(), &destination).await {
        Ok(()) => {}
        Err(_) if destination.is_dir() => {}
        Err(error) => return Err(error.into()),
    }
    Ok(destination)
}

fn repository_url(model: &str) -> Result<Url, TextBackendLoadError> {
    let mut url = Url::parse("https://modelscope.cn/api/v1/models/")
        .map_err(|_| TextBackendLoadError::ModelScopeListing)?;
    url.path_segments_mut().map_err(|_| TextBackendLoadError::ModelScopeListing)?
        .pop_if_empty().extend(model.split('/')).push("repo");
    Ok(url)
}

fn safe_path(value: &str) -> bool {
    !value.is_empty() && Path::new(value).components().all(|part| matches!(part, Component::Normal(_)))
}
