// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Target dispatch using the existing Frontend request and output processing.

use foretoken_llm_facade::LlmFacadeError;
use foretoken_llm_facade::draft_target::{
    DraftTargetRequest, DraftTargetSampling, RoleClient, TargetCommit,
};

/// Submits one normalized request and its Router-selected Draft set directly to Target.
pub async fn generate_draft_target_tokens(
    drafts: Vec<RoleClient>,
    target: RoleClient,
    request: vllm_llm::GenerateRequest,
) -> Result<foretoken_llm_facade::TokenStream, LlmFacadeError> {
    let input = normalize(&request)?;
    let model = target.status().await?.model;
    open_target(drafts, target, input, request, model)
        .await?
        .ok_or(LlmFacadeError::Unavailable)
}

/// Retains the Target-owned generation through frontend output and stop processing.
pub(crate) async fn open_target(
    drafts: Vec<RoleClient>,
    target: RoleClient,
    input: DraftTargetRequest,
    request: vllm_llm::GenerateRequest,
    model: String,
) -> Result<Option<foretoken_llm_facade::TokenStream>, LlmFacadeError> {
    let Some(mut session) = target
        .generate_with_drafts(&input, &request.request_id, &drafts)
        .await?
    else {
        return Ok(None);
    };
    let mut adapter = OutputAdapter::new(request);
    Ok(Some(Box::pin(async_stream::try_stream! {
        loop {
            let mut commit = session.next_commit().await?;
            if let Some(observation) = commit.round.take() {
                foretoken_metrics::record_draft_target_round(&model, &observation);
            }
            let finished = commit.finished;
            let output = adapter.convert(commit)?;
            if finished {
                session.close().await?;
                yield output;
                break;
            }
            yield output;
        }
    })))
}

/// Maps only supported normalized options; validation precedes remote admission.
pub(crate) fn normalize(
    request: &vllm_llm::GenerateRequest,
) -> Result<DraftTargetRequest, LlmFacadeError> {
    use foretoken_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
    let sampling = &request.sampling_params;
    // Explicit allowlist against upstream defaults: a newly added sampling option must not
    // silently acquire support through serialization.
    let supported = EngineCoreSamplingParams {
        temperature: sampling.temperature,
        top_p: sampling.top_p,
        top_k: sampling.top_k,
        seed: sampling.seed,
        max_tokens: sampling.max_tokens,
        min_tokens: sampling.min_tokens,
        stop_token_ids: sampling.stop_token_ids.clone(),
        eos_token_id: sampling.eos_token_id,
        all_stop_token_ids: sampling.all_stop_token_ids.clone(),
        ..Default::default()
    };
    if sampling != &supported {
        let message = if sampling.repetition_penalty != supported.repetition_penalty {
            "DT requires repetition_penalty=1.0; set it explicitly if the model's generation_config enables a penalty"
        } else if sampling.frequency_penalty != supported.frequency_penalty
            || sampling.presence_penalty != supported.presence_penalty
        {
            "DT requires frequency_penalty=0 and presence_penalty=0"
        } else if sampling.logprobs.is_some()
            || sampling.prompt_logprobs.is_some()
            || sampling.logprob_token_ids.is_some()
        {
            "DT does not return logprobs; remove logprobs, prompt_logprobs and logprob_token_ids"
        } else {
            "DT supports temperature, top_p, top_k, seed, max_tokens, min_tokens and stopping controls; remove other sampling options"
        };
        return Err(LlmFacadeError::UnsupportedRequest(message));
    }
    for (unsupported, message) in [
        (request.mm_features.is_some(), "DT supports text input only"),
        (
            request.lora_request.is_some(),
            "DT does not support LoRA requests",
        ),
        (
            request.reasoning_parser_kwargs.is_some(),
            "DT does not support reasoning_parser_kwargs",
        ),
        (
            request.cache_salt.is_some(),
            "DT does not support cache_salt; omit it",
        ),
        (
            request.data_parallel_rank.is_some_and(|rank| rank != 0),
            "DT requires data_parallel_rank=0; omit it or select rank 0",
        ),
        (
            request.trace_headers.is_some(),
            "DT does not support trace_headers",
        ),
    ] {
        if unsupported {
            return Err(LlmFacadeError::UnsupportedRequest(message));
        }
    }
    // The frontend has already resolved ignore_eos and extra EOS tokens. Disable Python's
    // independent EOS decision, then pass exactly the normalized stopping tokens.
    let eos = sampling.eos_token_id;
    let mut stop_tokens = sampling.stop_token_ids.clone();
    stop_tokens.extend(eos);
    stop_tokens.sort_unstable();
    stop_tokens.dedup();
    Ok(DraftTargetRequest {
        reservation: None,
        priority: request.priority,
        token_ids: request.prompt_token_ids.clone(),
        max_tokens: sampling.max_tokens,
        min_tokens: sampling.min_tokens,
        stop_token_ids: stop_tokens,
        ignore_eos: true,
        stop: Vec::new(),
        sampling: DraftTargetSampling {
            temperature: sampling.temperature,
            top_p: sampling.top_p,
            top_k: sampling.top_k,
            seed: sampling.seed,
        },
    })
}

struct OutputAdapter {
    request_id: String,
    eos: Option<u32>,
    prompt_info: Option<vllm_llm::GeneratePromptInfo>,
    cached_prompt_tokens: usize,
}

impl OutputAdapter {
    fn new(request: vllm_llm::GenerateRequest) -> Self {
        Self {
            request_id: request.request_id,
            eos: request.sampling_params.eos_token_id,
            cached_prompt_tokens: 0,
            prompt_info: Some(vllm_llm::GeneratePromptInfo {
                prompt_token_ids: request.prompt_token_ids.into(),
                prompt_logprobs: None,
            }),
        }
    }

    /// Preserves frontend stop semantics and emits prompt metadata once per member.
    fn convert(
        &mut self,
        commit: TargetCommit,
    ) -> Result<vllm_llm::GenerateOutput, LlmFacadeError> {
        if self.prompt_info.is_some() {
            self.cached_prompt_tokens = commit.cached_token_count;
        }
        use foretoken_engine_core_client::protocol::output::StopReason;
        use foretoken_llm_facade::draft_target::TargetStopReason;
        use vllm_llm::{FinishReason, GenerateOutput};
        let finish_reason = match (commit.finished, commit.finish_reason.as_deref()) {
            (false, None) => None,
            (true, Some("length")) => Some(FinishReason::Length),
            (true, Some("stop")) => Some(FinishReason::Stop(match commit.stop_reason {
                Some(TargetStopReason::TokenId(id)) if Some(id) == self.eos => None,
                Some(TargetStopReason::TokenId(id)) => Some(StopReason::TokenId(id)),
                Some(TargetStopReason::Text(text)) => Some(StopReason::Text(text)),
                None => None,
            })),
            _ => Err(LlmFacadeError::Protocol)?,
        };
        Ok(GenerateOutput {
            request_id: self.request_id.clone(),
            prompt_info: self.prompt_info.take(),
            token_ids: commit.token_ids,
            logprobs: None,
            finish_reason,
            cached_token_count: self.cached_prompt_tokens,
            kv_transfer_params: None,
            ec_transfer_params: None,
        })
    }
}
