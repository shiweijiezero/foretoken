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
    /// Role-resolved logical Pool names; empty retains the normal compatible range.
    pub allowed_pools: Vec<String>,
    /// Initial-stage candidates with explicit busy responses in the current dispatch round.
    pub busy_targets: Vec<(crate::RouteTargetId, u32)>,
    /// Tokenized vLLM request, including prompt tokens, sampling, multimodal, LoRA, and priority.
    pub generate_request: Option<Arc<vllm_llm::GenerateRequest>>,
    video_request_id: Option<String>,
}

impl RouterRequest {
    /// Creates a routing request from the selected model and tokenized generation request.
    pub fn new(model: impl Into<String>, generate_request: Arc<vllm_llm::GenerateRequest>) -> Self {
        Self {
            model: model.into(),
            allowed_pools: Vec::new(),
            busy_targets: Vec::new(),
            generate_request: Some(generate_request),
            video_request_id: None,
        }
    }

    /// Creates a video routing request without tokenization or KV-prefix semantics.
    pub fn video(model: impl Into<String>, request_id: String) -> Self {
        Self {
            model: model.into(),
            allowed_pools: Vec::new(),
            busy_targets: Vec::new(),
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

    /// Supplies the processed cache identity without transferring media tensors.
    pub fn kv_prefix_lookup<'a>(
        &'a self,
        route_target_id: &'a str,
        data_parallel_rank: u32,
    ) -> Result<KvPrefixLookup<'a>, KvPrefixUnavailableReason> {
        let request = self
            .generate_request
            .as_ref()
            .ok_or(KvPrefixUnavailableReason::UnsupportedRequest)?;
        if !self.reads_prefix_cache() {
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
            lora_name: request
                .lora_request
                .as_ref()
                .map(|lora| lora.lora_name.as_str()),
            lora_path: request
                .lora_request
                .as_ref()
                .map(|lora| lora.lora_path.as_str()),
            multimodal: request
                .mm_features
                .iter()
                .flatten()
                .map(|feature| foretoken_model_protocol::KvMultimodalIdentity {
                    identifier: feature.identifier.clone(),
                    offset: feature.mm_position.offset,
                    length: feature.mm_position.length,
                })
                .collect(),
        })
    }

    /// Reports whether this request permits native prefix-cache reads for routing estimates.
    pub(crate) fn reads_prefix_cache(&self) -> bool {
        self.generate_request.as_ref().is_some_and(|request| {
            !request
                .sampling_params
                .skip_reading_prefix_cache
                .unwrap_or(request.sampling_params.prompt_logprobs.is_some())
        })
    }

    /// Returns the prompt length used for route target input-limit matching.
    pub fn token_count(&self) -> usize {
        self.prompt_token_ids().len()
    }
}
