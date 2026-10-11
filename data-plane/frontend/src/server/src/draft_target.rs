// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request state, per-round placement and execution for independent DT roles.

use foretoken_llm_facade::LlmFacadeError;
use foretoken_llm_facade::draft_target::{
    DraftTargetRequest, DraftTargetSampling, RoleClient, TargetCommit,
};
use tokio_util::sync::CancellationToken;

mod round;
mod scheduler;
pub use round::{
    DraftTargetRequestState, DraftTargetRound, DraftTargetRoundOutput, DraftTargetRoundStream,
    select_draft_target_round,
};
pub use scheduler::DraftTargetScheduler;

/// Runs one request against explicitly supplied workers, for direct role integration.
///
/// The caller-selected workers are reused here. Batch schedulers instead call
/// `select_draft_target_round` after each round to choose placement again.
/// Dropping the output stream closes its request's remote sessions.
pub async fn generate_draft_target_tokens(
    draft: RoleClient,
    target: RoleClient,
    request: vllm_llm::GenerateRequest,
) -> Result<foretoken_llm_facade::TokenStream, LlmFacadeError> {
    use futures::StreamExt;
    let state = DraftTargetRequestState::new(request, CancellationToken::new())?;
    let first = DraftTargetRound::prepare(draft.clone(), target.clone(), vec![state], None).await?;
    Ok(Box::pin(async_stream::try_stream! {
        let mut round = first;
        loop {
            let mut stream = round.execute();
            let mut next = None;
            while let Some(event) = stream.next().await {
                let event = event?;
                next = event.continuation;
                if let Some(output) = event.output {
                    yield output;
                }
            }
            let Some(state) = next else {
                break;
            };
            round =
                DraftTargetRound::prepare(draft.clone(), target.clone(), vec![state], None).await?;
        }
    }))
}

/// Maps only supported normalized options; validation precedes routing and remote admission.
fn normalize(request: &vllm_llm::GenerateRequest) -> Result<DraftTargetRequest, LlmFacadeError> {
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
    if sampling != &supported
        || request.mm_features.is_some()
        || request.lora_request.is_some()
        || request.reasoning_parser_kwargs.is_some()
        || request.cache_salt.is_some()
        || request.priority != 0
        || request.data_parallel_rank.is_some_and(|rank| rank != 0)
        || request.trace_headers.is_some()
    {
        return Err(LlmFacadeError::InvalidRequest);
    }
    // The frontend has already resolved ignore_eos and extra EOS tokens. Disable Python's
    // independent EOS decision, then pass exactly the normalized stopping tokens.
    let eos = sampling.eos_token_id;
    let mut stop_tokens = sampling.stop_token_ids.clone();
    stop_tokens.extend(eos);
    stop_tokens.sort_unstable();
    stop_tokens.dedup();
    Ok(DraftTargetRequest {
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
