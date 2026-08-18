// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM text lowering reused by the Foretoken routing data path.

use std::collections::BTreeSet;
use std::path::Path;
use std::sync::Arc;

use foretoken_chat::{
    ChatBackend, ChatRequestProcessor, DynChatBackend, HfChatBackend, LoadModelBackendsOptions,
};
use foretoken_tokenizer::DynTokenizer;
use hf_hub::api::tokio::ApiBuilder;
use hf_hub::{Repo, RepoType, api::Siblings};
use thiserror::Error;
use vllm_engine_core_client::protocol::dtype::ModelDtype;
use vllm_text::backend::hf::HfTextBackend;

pub use vllm_text::*;

/// vLLM request processors constructed from one resolved snapshot.
pub struct HfSnapshotRuntime {
    pub text_processor: Arc<TextRequestProcessor>,
    pub tokenizer: DynTokenizer,
    pub chat_processor: Arc<ChatRequestProcessor>,
    pub supports_multimodal: bool,
}

const HF_TOKEN_ENV: &str = "HF_TOKEN";
const MODEL_FILES: &[&str] = &[
    "chat_template.json",
    "config.json",
    "generation_config.json",
    "preprocessor_config.json",
    "processor_config.json",
    "tekken.json",
    "tiktoken.model",
    "tokenizer.json",
    "tokenizer_config.json",
    "video_preprocessor_config.json",
];

/// Loads a local tokenizer directory or downloads a pinned Hub revision into the HF cache.
///
/// Remote files are placed in the standard `HF_HOME` cache and then loaded through vLLM's
/// local resolver so tokenizer selection remains upstream-owned.
pub async fn load_hf_text_backend(
    model_id: &str,
    revision: &str,
) -> std::result::Result<HfTextBackend, TextBackendLoadError> {
    if model_id.is_empty() || revision.is_empty() {
        return Err(TextBackendLoadError::MissingModelOrRevision);
    }
    if Path::new(model_id).is_dir() {
        return HfTextBackend::from_model(model_id)
            .await
            .map_err(|_| TextBackendLoadError::LocalModel);
    }

    let mut builder = ApiBuilder::from_env().with_progress(false);
    if let Ok(token) = std::env::var(HF_TOKEN_ENV)
        && !token.is_empty()
    {
        builder = builder.with_token(Some(token));
    }
    let api = builder
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
    let files = files_for_local_hf_resolver(&info.siblings);
    let mut snapshot_dir = None;

    for file in files {
        let path = repo
            .get(&file)
            .await
            .map_err(|_| TextBackendLoadError::Download { file: file.clone() })?;
        snapshot_dir = cache_snapshot_dir(&path);
    }

    let snapshot_dir = snapshot_dir.ok_or(TextBackendLoadError::NoTokenizerArtifact)?;
    let snapshot_dir = snapshot_dir
        .to_str()
        .ok_or(TextBackendLoadError::NonUtf8CachePath)?;
    HfTextBackend::from_model(snapshot_dir)
        .await
        .map_err(|_| TextBackendLoadError::CachedModel)
}

/// Builds text lowering and HF chat rendering from the same pinned local snapshot.
pub async fn load_hf_snapshot_runtime(
    model_id: &str,
    revision: &str,
    max_model_len: u32,
    model_dtype: ModelDtype,
) -> std::result::Result<HfSnapshotRuntime, TextBackendLoadError> {
    let text_backend = load_hf_text_backend(model_id, revision).await?;
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
    let supports_multimodal = chat_backend.multimodal_model_info().is_some();
    let text_backend: DynTextBackend = Arc::new(text_backend);
    let chat_backend: DynChatBackend = Arc::new(chat_backend);
    Ok(HfSnapshotRuntime {
        text_processor: Arc::new(TextRequestProcessor::new(text_backend, max_model_len)),
        tokenizer,
        chat_processor: Arc::new(ChatRequestProcessor::with_model_dtype(
            chat_backend,
            model_dtype,
        )),
        supports_multimodal,
    })
}

fn files_for_local_hf_resolver(siblings: &[Siblings]) -> Vec<String> {
    siblings
        .iter()
        .map(|sibling| sibling.rfilename.as_str())
        .filter(|name| {
            MODEL_FILES.contains(name) || name.ends_with(".tiktoken") || name.ends_with(".jinja")
        })
        .collect::<BTreeSet<_>>()
        .into_iter()
        .map(str::to_owned)
        .collect()
}

fn cache_snapshot_dir(path: &Path) -> Option<std::path::PathBuf> {
    path.ancestors()
        .find(|dir| {
            dir.parent()
                .is_some_and(|parent| parent.ends_with("snapshots"))
        })
        .map(Path::to_path_buf)
}

/// Failures preparing tokenizer artifacts at the frontend boundary.
#[derive(Debug, Error)]
pub enum TextBackendLoadError {
    #[error("tokenizer model and revision must not be empty")]
    MissingModelOrRevision,
    #[error("could not load tokenizer files from the local model directory")]
    LocalModel,
    #[error("could not initialize the Hugging Face client")]
    HubClient,
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
