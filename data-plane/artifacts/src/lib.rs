// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolve local model and tokenizer directories without changing remote model identifiers.

use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Model and tokenizer resolution selected by one ModelService.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum ModelSource {
    Local,
    Hf,
    ModelScope,
}

/// Environment contracts shared by workload projection and first-party source adapters.
pub const MODEL_ROOT_ENV: &str = "FORETOKEN_MODEL_ROOT";
// Must match the controller runtimeconfig producer.
pub const TEMPORARY_MODEL_ROOT_ENV: &str = "FORETOKEN_TEMPORARY_MODEL_ROOT";
pub const HF_TOKEN_ENV: &str = "HF_TOKEN";
pub const HF_HUB_OFFLINE_ENV: &str = "HF_HUB_OFFLINE";
pub const MODELSCOPE_CACHE_ENV: &str = "MODELSCOPE_CACHE";
pub const MODELSCOPE_DOMAIN_ENV: &str = "MODELSCOPE_DOMAIN";
pub const DEFAULT_MODELSCOPE_DOMAIN: &str = "www.modelscope.cn";

/// Returns the controller-projected persistent model root.
pub fn model_root() -> Option<PathBuf> {
    env_path(MODEL_ROOT_ENV)
}

/// Returns the Pod-scoped model root used when persistent frontend storage is unavailable.
pub fn temporary_model_root() -> Option<PathBuf> {
    env_path(TEMPORARY_MODEL_ROOT_ENV)
}

/// Returns the ModelScope SDK cache below one model root.
pub fn modelscope_cache_root(model_root: &Path) -> PathBuf {
    model_root.join("modelscope")
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
}

/// Resolve a directory for frontend or model-server, relative to an optional model root.
///
/// A missing relative directory leaves the identifier available for Hub resolution.
/// Absolute directories keep their Pod-local meaning. Relative links must stay inside
/// the model root; individual files are not replaced by their parent directory. The inference
/// engine owns validation of the files within a resolved model or tokenizer directory.
pub fn resolve_directory(root: Option<&Path>, identifier: &str) -> io::Result<Option<PathBuf>> {
    let path = Path::new(identifier);
    if path.is_absolute() {
        return if path.is_dir() {
            Ok(Some(path.to_path_buf()))
        } else {
            Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("local artifact {identifier:?} must be a model or tokenizer directory"),
            ))
        };
    }
    let Some(root) = root else {
        if path.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("local artifact {identifier:?} must be a model or tokenizer directory"),
            ));
        }
        return Ok(path.is_dir().then(|| path.to_path_buf()));
    };
    if path
        .components()
        .any(|component| matches!(component, Component::ParentDir))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("artifact {identifier:?} must stay below the model directory"),
        ));
    }
    let directory = match fs::canonicalize(root.join(path)) {
        Ok(directory) => directory,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    if !directory.starts_with(fs::canonicalize(root)?) || !directory.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("artifact {identifier:?} must be a directory within the model root"),
        ));
    }
    Ok(Some(directory))
}
