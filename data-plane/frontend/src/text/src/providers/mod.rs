// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolves source-specific metadata while preserving the controller's fixed tokenizer identity.

mod huggingface;
pub(super) mod modelscope;

use std::path::{Path, PathBuf};

use foretoken_artifacts::{MODEL_METADATA_FILES, ModelSource};
use foretoken_model_protocol::PreparedTokenizer;

use crate::TextBackendLoadError;

/// Resolves a directory for processor construction without transferring provider state to the runtime.
pub(super) async fn resolve_snapshot(
    source: ModelSource,
    model_id: &str,
    revision: &str,
    prepared: Option<&PreparedTokenizer>,
) -> Result<PathBuf, TextBackendLoadError> {
    if let Some(prepared) = prepared {
        return resolve_prepared_snapshot(source, model_id, revision, prepared).await;
    }
    match source {
        ModelSource::Local => {
            let model_root = foretoken_artifacts::model_root();
            foretoken_artifacts::resolve_directory(model_root.as_deref(), model_id)
                .map_err(TextBackendLoadError::LocalModelPath)?
                .ok_or(TextBackendLoadError::LocalModelNotFound)
        }
        ModelSource::Hf => huggingface::resolve_snapshot(model_id, revision).await,
        ModelSource::ModelScope => {
            Ok(modelscope::resolve_snapshot(model_id, revision, MODEL_METADATA_FILES).await?)
        }
    }
}

/// Reuses an exact shared publication or fetches only its recorded metadata file revisions.
async fn resolve_prepared_snapshot(
    source: ModelSource,
    model_id: &str,
    revision: &str,
    prepared: &PreparedTokenizer,
) -> Result<PathBuf, TextBackendLoadError> {
    if prepared.model != model_id || prepared.revision != revision || prepared.files.is_empty() {
        return Err(TextBackendLoadError::PreparedIdentity);
    }
    let same_binding = prepared.cache_binding.as_ref().is_some_and(|binding| {
        std::env::var(foretoken_artifacts::RUNTIME_CACHE_BINDING_ENV).as_ref() == Ok(binding)
    });
    let directory = Path::new(&prepared.directory);
    // Only the same persistent claim/layout may reuse provider-owned paths directly.
    if same_binding
        && let Some(root) = foretoken_artifacts::model_cache_root()
        && directory.is_absolute()
        && directory.starts_with(&root)
    {
        match std::fs::canonicalize(directory) {
            Ok(directory) => {
                let root =
                    std::fs::canonicalize(root).map_err(TextBackendLoadError::LocalModelPath)?;
                if !directory.starts_with(root) {
                    return Err(TextBackendLoadError::PreparedPath);
                }
                if prepared
                    .files
                    .keys()
                    .all(|file| directory.join(file).is_file())
                {
                    return Ok(directory);
                }
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(TextBackendLoadError::LocalModelPath(error)),
        }
    }
    match source {
        ModelSource::Hf => huggingface::resolve_prepared_snapshot(model_id, prepared).await,
        ModelSource::ModelScope => Ok(modelscope::resolve_prepared_snapshot(prepared).await?),
        ModelSource::Local => Err(TextBackendLoadError::PreparedIdentity),
    }
}
