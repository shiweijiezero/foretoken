// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! The minimal request data used for route target selection.

use std::sync::Arc;

/// Request information available to every routing algorithm stage.
#[derive(Clone)]
pub struct RouterRequest {
    /// Requested logical model name.
    pub model: String,
    /// Requested model revision, or `None` when any ready revision may serve it.
    pub revision: Option<String>,
    /// Tokenized vLLM request, including prompt tokens, sampling, multimodal, LoRA, and priority.
    pub generate_request: Arc<vllm_llm::GenerateRequest>,
}

impl RouterRequest {
    /// Creates a routing request from the selected model and tokenized generation request.
    pub fn new(
        model: impl Into<String>,
        revision: Option<String>,
        generate_request: Arc<vllm_llm::GenerateRequest>,
    ) -> Self {
        Self {
            model: model.into(),
            revision,
            generate_request,
        }
    }

    /// Returns the prompt tokens used by KV-prefix algorithms.
    pub fn prompt_token_ids(&self) -> &[u32] {
        &self.generate_request.prompt_token_ids
    }

    /// Returns the prompt length used for route target input-limit matching.
    pub fn token_count(&self) -> usize {
        self.generate_request.prompt_token_ids.len()
    }
}
