// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Composes vLLM text and chat processors from provider-resolved model metadata.

mod providers;

use std::path::Path;
use std::sync::Arc;

use foretoken_artifacts::ModelSource;
use foretoken_chat::{
    ChatBackend, ChatRequestProcessor, DynChatBackend, HfChatBackend, LoadModelBackendsOptions,
};
use foretoken_engine_core_client::protocol::dtype::ModelDtype;
use foretoken_model_protocol::PreparedTokenizer;
use foretoken_tokenizer::DynTokenizer;
use thiserror::Error;
use vllm_text::backend::hf::HfTextBackend;

pub use vllm_text::*;

/// vLLM request processors constructed from one resolved model-source snapshot.
pub struct SnapshotRuntime {
    pub text_processor: Arc<TextRequestProcessor>,
    pub tokenizer: DynTokenizer,
    pub chat_processor: Arc<ChatRequestProcessor>,
    pub supports_multimodal: bool,
}

/// Failures preparing tokenizer artifacts at the frontend boundary.
#[derive(Debug, Error)]
pub enum TextBackendLoadError {
    #[error("prepared tokenizer does not match the serving snapshot")]
    PreparedIdentity,
    #[error("prepared tokenizer directory is outside the shared model root")]
    PreparedPath,
    #[error("tokenizer model and revision must not be empty")]
    MissingModelOrRevision,
    #[error("could not load tokenizer files from the local model directory")]
    LocalModel,
    #[error("local model or tokenizer directory was not found")]
    LocalModelNotFound,
    #[error("invalid local model path: {0}")]
    LocalModelPath(#[source] std::io::Error),
    #[error(transparent)]
    ModelScope(#[from] providers::modelscope::ModelScopeError),
    #[error("could not initialize the Hugging Face client")]
    HubClient,
    #[error("Hugging Face snapshot is not available in the offline cache")]
    OfflineCacheMiss,
    #[error("could not retrieve Hugging Face repository metadata")]
    RepositoryInfo,
    #[error("could not download required tokenizer artifact {file}")]
    Download { file: String },
    #[error("Hugging Face repository has no supported tokenizer artifact")]
    NoTokenizerArtifact,
    #[error("Hugging Face cache path is not UTF-8")]
    NonUtf8CachePath,
    #[error("could not load tokenizer files from the Hugging Face cache")]
    CachedModel,
}

/// Resolves metadata through the selected source and loads the text backend for standalone callers.
pub async fn load_text_backend(
    source: ModelSource,
    model_id: &str,
    revision: &str,
) -> std::result::Result<HfTextBackend, TextBackendLoadError> {
    if model_id.is_empty() || revision.is_empty() {
        return Err(TextBackendLoadError::MissingModelOrRevision);
    }
    let directory = providers::resolve_snapshot(source, model_id, revision, None).await?;
    load_resolved_backend(source, &directory).await
}

/// Builds text lowering and chat rendering from the same pinned local metadata snapshot.
/// RuntimeBuilder retains the returned processors for the prepared serving generation.
pub async fn load_snapshot_runtime(
    source: ModelSource,
    model_id: &str,
    revision: &str,
    max_model_len: u32,
    max_logprobs: Option<i32>,
    model_dtype: Option<ModelDtype>,
    prepared: Option<&PreparedTokenizer>,
) -> std::result::Result<SnapshotRuntime, TextBackendLoadError> {
    let text_backend = match prepared {
        Some(prepared) => {
            let directory =
                providers::resolve_snapshot(source, model_id, revision, Some(prepared)).await?;
            load_resolved_backend(source, &directory).await?
        }
        None => load_text_backend(source, model_id, revision).await?,
    };
    let tokenizer = text_backend.tokenizer();
    let chat_backend = HfChatBackend::from_resolved_model_files(
        text_backend.resolved_model_files().clone(),
        model_id.to_owned(),
        LoadModelBackendsOptions {
            language_model_only: false,
            ..Default::default()
        },
        tokenizer.clone(),
    )
    .map_err(|_| TextBackendLoadError::CachedModel)?;
    let supports_multimodal =
        chat_backend.multimodal_model_info().is_some() && model_dtype.is_some();
    let text_backend: DynTextBackend = Arc::new(text_backend);
    let chat_backend: DynChatBackend = Arc::new(chat_backend);
    let chat_processor = match model_dtype {
        Some(model_dtype) => ChatRequestProcessor::with_model_dtype(chat_backend, model_dtype),
        None => ChatRequestProcessor::render_only(chat_backend),
    };
    Ok(SnapshotRuntime {
        text_processor: Arc::new(
            TextRequestProcessor::new(text_backend, max_model_len).with_max_logprobs(max_logprobs),
        ),
        tokenizer,
        chat_processor: Arc::new(chat_processor),
        supports_multimodal,
    })
}

/// Loads upstream processors only after the provider has resolved a complete metadata directory.
async fn load_resolved_backend(
    source: ModelSource,
    directory: &Path,
) -> std::result::Result<HfTextBackend, TextBackendLoadError> {
    let directory = directory
        .to_str()
        .ok_or(TextBackendLoadError::NonUtf8CachePath)?;
    HfTextBackend::from_model(directory).await.map_err(|_| {
        if source == ModelSource::Local {
            TextBackendLoadError::LocalModel
        } else {
            TextBackendLoadError::CachedModel
        }
    })
}
