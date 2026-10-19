// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolve local model and tokenizer directories without changing remote model identifiers.

pub mod source;

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

/// Model and tokenizer inputs shared by preparation workers and inference-engine launchers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelArtifacts {
    pub model: String,
    pub source: ModelSource,
    pub revision: String,
    pub tokenizer: String,
    #[serde(rename = "tokenizerRevision")]
    pub tokenizer_revision: String,
}

/// Environment contracts shared by workload projection and first-party source adapters.
pub const MODEL_ARTIFACTS_ENV: &str = "FORETOKEN_MODEL_ARTIFACTS";
pub const MODEL_ROOT_ENV: &str = "FORETOKEN_MODEL_ROOT";
pub const MODEL_CACHE_ROOT_ENV: &str = "FORETOKEN_MODEL_CACHE_ROOT";
pub const PREPARATION_ROOT_ENV: &str = "FORETOKEN_MODEL_PREPARATION_ROOT";
pub const HF_HUB_CACHE_ENV: &str = "HF_HUB_CACHE";
// Must match the controller runtimeconfig producer.
pub const TEMPORARY_MODEL_ROOT_ENV: &str = "FORETOKEN_TEMPORARY_MODEL_ROOT";
pub const HF_TOKEN_ENV: &str = "HF_TOKEN";
pub const HF_HUB_OFFLINE_ENV: &str = "HF_HUB_OFFLINE";
pub const MODELSCOPE_CACHE_ENV: &str = "MODELSCOPE_CACHE";
pub const MODELSCOPE_DOMAIN_ENV: &str = "MODELSCOPE_DOMAIN";
pub const DEFAULT_MODELSCOPE_DOMAIN: &str = "www.modelscope.cn";
/// Claim and layout identity projected only when a workload mounts a persistent RuntimeCache.
pub const RUNTIME_CACHE_BINDING_ENV: &str = "FORETOKEN_RUNTIME_CACHE_BINDING";

/// Files consumed by frontend tokenizer, model config, and chat-template loading.
pub const MODEL_METADATA_FILES: &[&str] = &[
    "added_tokens.json",
    "chat_template.json",
    "config.json",
    "generation_config.json",
    "merges.txt",
    "preprocessor_config.json",
    "processor_config.json",
    "sentencepiece.bpe.model",
    "special_tokens_map.json",
    "spiece.model",
    "tekken.json",
    "tiktoken.model",
    "tokenizer.json",
    "tokenizer.model",
    "tokenizer_config.json",
    "video_preprocessor_config.json",
    "vocab.json",
    "vocab.txt",
];

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
            let directory = fs::canonicalize(path)?;
            validate_local_storage(&directory)?;
            Ok(Some(directory))
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
        return if path.is_dir() {
            let directory = fs::canonicalize(path)?;
            validate_local_storage(&directory)?;
            Ok(Some(directory))
        } else {
            Ok(None)
        };
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
    validate_local_storage(&directory)?;
    Ok(Some(directory))
}

impl ModelArtifacts {
    /// Reads the controller's engine-neutral selection for a preparation Job.
    pub fn from_env() -> io::Result<Self> {
        let input = std::env::var(MODEL_ARTIFACTS_ENV).map_err(io::Error::other)?;
        let artifacts: Self = serde_json::from_str(&input).map_err(io::Error::other)?;
        artifacts.validate().map_err(io::Error::other)?;
        Ok(artifacts)
    }

    /// Checks required artifact identities before preparation or engine startup consumes them.
    pub fn validate(&self) -> Result<(), String> {
        for (name, value) in [
            ("model", &self.model),
            ("revision", &self.revision),
            ("tokenizer", &self.tokenizer),
            ("tokenizerRevision", &self.tokenizer_revision),
        ] {
            if value.is_empty() {
                return Err(format!("model artifacts.{name} must be nonempty"));
            }
        }
        Ok(())
    }
}

/// Resolves one publication scope within the caller's namespace-private preparation root.
/// Model preparation and frontend metadata staging share this directory boundary.
pub fn preparation_directory(root: &Path, scope: &str) -> io::Result<PathBuf> {
    let relative = Path::new(scope);
    if scope.is_empty()
        || relative
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "invalid model preparation scope",
        ));
    }
    Ok(root.join(relative))
}

/// Returns the controller-projected root for user-provided local models.
pub fn model_root() -> Option<PathBuf> {
    env_path(MODEL_ROOT_ENV)
}

/// Returns provider download storage, using the model root for standalone callers without a separate cache.
pub fn model_cache_root() -> Option<PathBuf> {
    env_path(MODEL_CACHE_ROOT_ENV).or_else(model_root)
}

/// Returns the namespace-private publication root for managed model and tokenizer preparation.
pub fn preparation_root() -> Option<PathBuf> {
    env_path(PREPARATION_ROOT_ENV)
}

/// Returns an explicit Hub file-cache location independently of the caller's HF account directory.
pub fn huggingface_cache_root() -> Option<PathBuf> {
    env_path(HF_HUB_CACHE_ENV)
}

/// Returns the Pod-scoped model root used when persistent frontend storage is unavailable.
pub fn temporary_model_root() -> Option<PathBuf> {
    env_path(TEMPORARY_MODEL_ROOT_ENV)
}

/// Returns the ModelScope SDK cache below one model root.
pub fn modelscope_cache_root(model_root: &Path) -> PathBuf {
    model_root.join("modelscope")
}

// Managed downloads have source-owned lifetimes. A local model must keep its own
// storage rather than bypassing those references through a cache path or directory link.
fn validate_local_storage(directory: &Path) -> io::Result<()> {
    let Some(cache) = env_path(MODEL_CACHE_ROOT_ENV) else {
        return Ok(());
    };
    let cache = match fs::canonicalize(cache) {
        Ok(cache) => cache,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if fs::canonicalize(directory)?.starts_with(cache) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "local model and tokenizer directories must be outside the managed download cache; use source hf or modelscope to share cached downloads",
        ));
    }
    Ok(())
}

fn env_path(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .filter(|path| !path.is_empty())
        .map(PathBuf::from)
}
