// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! The minimal request data used for route target selection.

use std::sync::Arc;

use foretoken_kv_indexer::{KvPrefixLookup, KvPrefixUnavailableReason};

/// Request information available to every routing algorithm stage.
#[derive(Clone)]
pub struct RouterRequest {
    /// Requested logical model name.
    pub model: String,
    /// Tokenized vLLM request, including prompt tokens, sampling, multimodal, LoRA, and priority.
    pub generate_request: Arc<vllm_llm::GenerateRequest>,
}

impl RouterRequest {
    /// Creates a routing request from the selected model and tokenized generation request.
    pub fn new(model: impl Into<String>, generate_request: Arc<vllm_llm::GenerateRequest>) -> Self {
        Self {
            model: model.into(),
            generate_request,
        }
    }

    /// Returns the prompt tokens used by KV-prefix algorithms.
    pub fn prompt_token_ids(&self) -> &[u32] {
        &self.generate_request.prompt_token_ids
    }

    /// Rejects request features whose cache identity is not represented by prompt tokens.
    pub fn kv_prefix_lookup<'a>(
        &'a self,
        route_target_id: &'a str,
        data_parallel_rank: u32,
    ) -> Result<KvPrefixLookup<'a>, KvPrefixUnavailableReason> {
        if self.generate_request.cache_salt.is_some()
            || self.generate_request.lora_request.is_some()
            || self.generate_request.mm_features.is_some()
            || self
                .generate_request
                .sampling_params
                .skip_reading_prefix_cache
                == Some(true)
        {
            return Err(KvPrefixUnavailableReason::UnsupportedRequest);
        }

        Ok(KvPrefixLookup::new(
            route_target_id,
            data_parallel_rank,
            self.prompt_token_ids(),
        ))
    }

    /// Returns the prompt length used for route target input-limit matching.
    pub fn token_count(&self) -> usize {
        self.generate_request.prompt_token_ids.len()
    }
}
