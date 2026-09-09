// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared Hugging Face endpoint selection for model and frontend downloads.

use std::time::Duration;

use std::path::{Path, PathBuf};
use reqwest::{Client, StatusCode, Url};
use thiserror::Error;

const HF_ENDPOINT: &str = "https://huggingface.co";
const HF_MIRROR: &str = "https://hf-mirror.com";

/// Selects a reachable endpoint for public model downloads in the current runtime network.
///
/// Frontend loading and engine startup share this decision. Explicit endpoints, offline mode,
/// and credentials retain their configured origin; automatic mirrors never receive a Hub token.
pub async fn hugging_face_endpoint(
    model: &str,
    revision: &str,
    cache_home: &Path,
) -> Result<String, SourceError> {
    if let Ok(endpoint) = std::env::var("HF_ENDPOINT")
        && !endpoint.is_empty()
    {
        return Ok(endpoint);
    }
    let has_token = ["HF_TOKEN", "HUGGING_FACE_HUB_TOKEN"].iter().any(|name| {
        std::env::var(name).is_ok_and(|value| !value.is_empty())
    }) || std::fs::read_to_string(cache_home.join("token")).is_ok_and(|value| !value.trim().is_empty())
        || std::fs::read_to_string(hugging_face_home().join("token")).is_ok_and(|value| !value.trim().is_empty())
        || std::env::var_os("HF_TOKEN_PATH").is_some_and(|path| {
            std::fs::read_to_string(path).is_ok_and(|value| !value.trim().is_empty())
        });
    if has_token || offline() || std::path::Path::new(model).is_dir() {
        return Ok(HF_ENDPOINT.into());
    }

    let client = Client::builder().timeout(Duration::from_secs(5)).build()?;
    let origin = client.head(config_url(HF_ENDPOINT, model, revision)?).send().await;
    match origin {
        // Missing repositories, access restrictions, and throttling are not network failures.
        Ok(response) if !response.status().is_server_error() => return Ok(HF_ENDPOINT.into()),
        Err(error) if !(error.is_connect() || error.is_timeout() || error.is_request()) => {
            return Err(error.into());
        }
        _ => {}
    }
    let response = client.head(config_url(HF_MIRROR, model, revision)?).send().await?;
    if response.status() != StatusCode::OK {
        return Err(SourceError::MirrorUnavailable(response.status()));
    }
    tracing::info!(endpoint = HF_MIRROR, "Hugging Face is unreachable; using the public mirror");
    Ok(HF_MIRROR.into())
}

/// Returns the standard Hub home used to locate credentials before spawning Python.
pub fn hugging_face_home() -> PathBuf {
    std::env::var_os("HF_HOME").map(PathBuf::from).unwrap_or_else(|| {
        std::env::var_os("XDG_CACHE_HOME").map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from(std::env::var_os("HOME").unwrap_or_default()).join(".cache"))
            .join("huggingface")
    })
}

/// Reports Hub offline mode to callers that must load only existing model files.
pub fn offline() -> bool {
    std::env::var("HF_HUB_OFFLINE")
        .is_ok_and(|value| matches!(value.to_ascii_uppercase().as_str(), "1" | "ON" | "YES" | "TRUE"))
}

fn config_url(endpoint: &str, model: &str, revision: &str) -> Result<Url, SourceError> {
    let mut url = Url::parse(endpoint).map_err(|_| SourceError::InvalidEndpoint)?;
    url.path_segments_mut().map_err(|_| SourceError::InvalidEndpoint)?
        .extend(model.split('/')).push("resolve").push(revision).push("config.json");
    Ok(url)
}

/// Failures selecting the model download origin before a runtime is started.
#[derive(Debug, Error)]
pub enum SourceError {
    #[error("model source request failed: {0}")]
    Request(#[from] reqwest::Error),
    #[error("invalid model source endpoint")]
    InvalidEndpoint,
    #[error("Hugging Face is unreachable and the public mirror returned {0}")]
    MirrorUnavailable(StatusCode),
}
