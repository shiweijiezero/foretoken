// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! The minimal request data used for route target selection.

use std::sync::Arc;

use foretoken_kv_indexer::{KvPrefixLookup, KvPrefixUnavailableReason};

/// Preprocessed request information available to the target-selection stages.
#[derive(Clone)]
pub struct RouterRequest {
    /// Requested logical model name.
    pub model: String,
    /// Tokenized vLLM request, including prompt tokens, sampling, multimodal, LoRA, and priority.
    pub generate_request: Option<Arc<vllm_llm::GenerateRequest>>,
    video_request_id: Option<String>,
}

impl RouterRequest {
    /// Creates a routing request from the selected model and tokenized generation request.
    pub fn new(model: impl Into<String>, generate_request: Arc<vllm_llm::GenerateRequest>) -> Self {
        Self {
            model: model.into(),
            generate_request: Some(generate_request),
            video_request_id: None,
        }
    }

    /// Creates a video routing request without tokenization or KV-prefix semantics.
    pub fn video(model: impl Into<String>, request_id: String) -> Self {
        Self {
            model: model.into(),
            generate_request: None,
            video_request_id: Some(request_id),
        }
    }

    /// Returns the request identity retained by routing reservations.
    pub fn request_id(&self) -> &str {
        match &self.generate_request {
            Some(request) => request.request_id.as_str(),
            None => self
                .video_request_id
                .as_deref()
                .expect("video request identity"),
        }
    }

    /// Returns the prompt tokens used by KV-prefix algorithms.
    pub fn prompt_token_ids(&self) -> &[u32] {
        self.generate_request
            .as_ref()
            .map_or(&[], |request| &request.prompt_token_ids)
    }

    /// Supplies token and salt identity, excluding unsupported multimodal and adapter semantics.
    pub fn kv_prefix_lookup<'a>(
        &'a self,
        route_target_id: &'a str,
        data_parallel_rank: u32,
    ) -> Result<KvPrefixLookup<'a>, KvPrefixUnavailableReason> {
        let request = self
            .generate_request
            .as_ref()
            .ok_or(KvPrefixUnavailableReason::UnsupportedRequest)?;
        if request.lora_request.is_some()
            || request.mm_features.is_some()
            || request.sampling_params.skip_reading_prefix_cache == Some(true)
        {
            return Err(KvPrefixUnavailableReason::UnsupportedRequest);
        }

        Ok(KvPrefixLookup {
            route_target_id,
            data_parallel_rank,
            prompt_token_ids: self.prompt_token_ids(),
            cache_salt: request
                .cache_salt
                .as_deref()
                .filter(|salt| !salt.is_empty()),
        })
    }

    /// Returns the prompt length used for route target input-limit matching.
    pub fn token_count(&self) -> usize {
        self.prompt_token_ids().len()
    }
}
