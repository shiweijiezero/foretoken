// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM text lowering reused by the Foretoken routing data path.

mod modelscope;

use foretoken_model_protocol::ModelSource;
use std::collections::BTreeSet;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use foretoken_chat::{
    ChatBackend, ChatRequestProcessor, DynChatBackend, HfChatBackend, LoadModelBackendsOptions,
};
use foretoken_engine_core_client::protocol::dtype::ModelDtype;
use foretoken_tokenizer::DynTokenizer;
use hf_hub::api::tokio::ApiBuilder;
use hf_hub::{Cache, Repo, RepoType, api::Siblings};
use thiserror::Error;
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
const RUNTIME_CACHE_ROOT_ENV: &str = "FORETOKEN_CACHE_MOUNT_PATH";
const TEMPORARY_CACHE_ROOT_ENV: &str = "FORETOKEN_TEMPORARY_CACHE_ROOT";
const MODEL_FILES: &[&str] = &[
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

/// Loads a local tokenizer directory or downloads a pinned Hub revision into the HF cache.
///
/// Remote files use the standard `HF_HOME` cache, or the controller-projected Pod cache when
/// persistent storage is offline, and are then loaded through vLLM's local resolver.
pub async fn load_text_backend(
    model_id: &str,
    revision: &str,
    source: ModelSource,
    source_endpoint: Option<&str>,
) -> std::result::Result<HfTextBackend, TextBackendLoadError> {
    if model_id.is_empty() || revision.is_empty() {
        return Err(TextBackendLoadError::MissingModelOrRevision);
    }
    if Path::new(model_id).is_dir() {
        return HfTextBackend::from_model(model_id)
            .await
            .map_err(|_| TextBackendLoadError::LocalModel);
    }
    if source == ModelSource::ModelScope {
        let cache = std::env::var_os(TEMPORARY_CACHE_ROOT_ENV)
            .or_else(|| std::env::var_os(RUNTIME_CACHE_ROOT_ENV))
            .map(PathBuf::from)
            .unwrap_or_else(|| Cache::from_env().path().clone());
        let snapshot = modelscope::snapshot(model_id, revision, &cache).await?;
        return HfTextBackend::from_model(snapshot.to_str().ok_or(TextBackendLoadError::NonUtf8CachePath)?)
            .await.map_err(|_| TextBackendLoadError::CachedModel);
    }
    if let Some(snapshot) = cached_model_snapshot(model_id, revision) {
        let snapshot = snapshot
            .to_str()
            .ok_or(TextBackendLoadError::NonUtf8CachePath)?;
        return HfTextBackend::from_model(snapshot)
            .await
            .map_err(|_| TextBackendLoadError::CachedModel);
    }
    if foretoken_model_source::offline() {
        return Err(TextBackendLoadError::OfflineCacheMiss);
    }

    let hub_cache = runtime_hf_cache();
    let endpoint = match source_endpoint {
        Some(endpoint) => endpoint.to_owned(),
        None => foretoken_model_source::hugging_face_endpoint(
            model_id, revision, hub_cache.token_path().parent().ok_or(TextBackendLoadError::NonUtf8CachePath)?,
        ).await?,
    };
    let credential_endpoint = std::env::var("HF_ENDPOINT").ok().filter(|value| !value.is_empty())
        .unwrap_or_else(|| "https://huggingface.co".into());
    let send_credentials = endpoint.trim_end_matches('/') == credential_endpoint.trim_end_matches('/');
    let mut builder = ApiBuilder::from_env()
        .with_endpoint(endpoint)
        .with_cache_dir(runtime_hf_cache().path().clone())
        .with_progress(false);
    if let Some(cache_root) = std::env::var(TEMPORARY_CACHE_ROOT_ENV)
        .ok()
        .filter(|path| !path.is_empty())
    {
        builder = builder.with_cache_dir(hf_cache_dir(PathBuf::from(cache_root)));
    }
    if !send_credentials {
        builder = builder.with_token(None);
    }
    if let Ok(token) = std::env::var(HF_TOKEN_ENV)
        && send_credentials
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
pub async fn load_model_runtime(
    model_id: &str,
    revision: &str,
    source: ModelSource,
    source_endpoint: Option<&str>,
    max_model_len: u32,
    model_dtype: Option<ModelDtype>,
) -> std::result::Result<HfSnapshotRuntime, TextBackendLoadError> {
    let text_backend = load_text_backend(model_id, revision, source, source_endpoint).await?;
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
    Ok(HfSnapshotRuntime {
        text_processor: Arc::new(TextRequestProcessor::new(text_backend, max_model_len)),
        tokenizer,
        chat_processor: Arc::new(chat_processor),
        supports_multimodal,
    })
}

fn hf_cache_dir(root: PathBuf) -> PathBuf {
    root.join("models/hub")
}

fn runtime_hf_cache() -> Cache {
    std::env::var_os(RUNTIME_CACHE_ROOT_ENV)
        .map(PathBuf::from)
        .map(hf_cache_dir)
        .map(Cache::new)
        .unwrap_or_else(Cache::from_env)
}

fn cached_model_snapshot(model_id: &str, revision: &str) -> Option<std::path::PathBuf> {
    let repo = runtime_hf_cache().repo(Repo::with_revision(
        model_id.to_owned(),
        RepoType::Model,
        revision.to_owned(),
    ));
    MODEL_FILES
        .iter()
        .find_map(|file| repo.get(file))?
        .parent()
        .map(Path::to_path_buf)
}

fn is_model_file(name: &str) -> bool {
    MODEL_FILES.contains(&name) || name.ends_with(".tiktoken") || name.ends_with(".jinja")
}

fn files_for_local_hf_resolver(siblings: &[Siblings]) -> Vec<String> {
    siblings
        .iter()
        .map(|sibling| sibling.rfilename.as_str())
        .filter(|name| is_model_file(name))
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
    #[error(transparent)]
    Source(#[from] foretoken_model_source::SourceError),
    #[error("ModelScope request failed: {0}")]
    ModelScopeRequest(#[from] reqwest::Error),
    #[error("ModelScope repository returned an invalid file listing")]
    ModelScopeListing,
    #[error("could not prepare model files: {0}")]
    Storage(#[from] std::io::Error),
    #[error("tokenizer model and revision must not be empty")]
    MissingModelOrRevision,
    #[error("could not load tokenizer files from the local model directory")]
    LocalModel,
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
