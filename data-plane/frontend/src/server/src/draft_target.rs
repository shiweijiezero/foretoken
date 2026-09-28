// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! DT workflow for selected role endpoints; no placement policy lives here.

use std::pin::Pin;

use foretoken_llm_facade::LlmFacadeError;
use foretoken_llm_facade::draft_target::{
    DraftTargetRequest, DraftTargetSampling, RoleClient, RoleSession, TargetCommit,
};
use futures::Stream;

/// Streams only confirmed Target output while retaining both remote session lifetimes.
pub type DraftTargetStream =
    Pin<Box<dyn Stream<Item = Result<TargetCommit, LlmFacadeError>> + Send>>;

/// Runs one request against caller-selected roles.
///
/// Frontend callers own tokenization, compatible model selection and the request deadline.
/// The returned stream owns round ordering and cancellation: dropping it closes both role
/// sessions, including while awaiting a proposal. No background task outlives its consumer.
pub async fn generate_draft_target(
    draft: RoleClient,
    target: RoleClient,
    request: DraftTargetRequest,
) -> Result<DraftTargetStream, LlmFacadeError> {
    let (draft_status, target_status) = tokio::try_join!(draft.status(), target.status())?;
    if draft_status.role != "draft"
        || target_status.role != "target"
        || draft_status.candidate_format != target_status.candidate_format
        || !matches!(
            draft_status.candidate_format.as_str(),
            "greedy_token_ids" | "token_ids_log_probs"
        )
    {
        return Err(LlmFacadeError::Configuration);
    }
    if request.sampling.temperature != 0.0 && draft_status.candidate_format != "token_ids_log_probs"
    {
        return Err(LlmFacadeError::InvalidRequest);
    }
    if !draft_status.accepting || !target_status.accepting {
        return Err(LlmFacadeError::Unavailable);
    }
    let budget = draft_status.token_budget.min(target_status.token_budget);
    if budget == 0 {
        return Err(LlmFacadeError::Configuration);
    }
    let context_limit = draft_status.max_model_len.min(target_status.max_model_len) as usize;
    if request
        .token_ids
        .len()
        .saturating_add(request.max_tokens as usize)
        > context_limit
    {
        return Err(LlmFacadeError::InvalidRequest);
    }
    let mut target = target.generate(&request).await?;
    Ok(Box::pin(async_stream::try_stream! {
        let mut draft_session: Option<(RoleSession, u64)> = None;
        let mut remaining = request.max_tokens;
        loop {
            let commit = target.next_commit().await?;
            remaining = remaining.checked_sub(u32::try_from(commit.token_ids.len())
                .map_err(|_| LlmFacadeError::Protocol)?).ok_or(LlmFacadeError::Protocol)?;
            if commit.finished {
                if commit.version.is_some() || commit.finish_reason.is_none() {
                    Err(LlmFacadeError::Protocol)?;
                }
                // Release remote ownership before exposing terminal output, even if the
                // consumer keeps the exhausted stream without polling it again.
                drop(draft_session.take());
                drop(target);
                yield commit;
                break;
            }
            if remaining == 0 { Err(LlmFacadeError::Protocol)?; }
            let version = commit.version.ok_or(LlmFacadeError::Protocol)?;
            if let Some((session, previous)) = &mut draft_session {
                session.commit(*previous, version, &commit.token_ids).await?;
                *previous = version;
            } else {
                let mut prefix = request.token_ids.clone();
                prefix.extend_from_slice(&commit.token_ids);
                draft_session = Some((draft.bind(&prefix, version, &request.sampling).await?, version));
            }
            // Backpressure belongs to the caller. Do not spawn speculative work detached
            // from the output stream; cancellation must drop the session while a round awaits.
            yield commit;
            let (session, _) = draft_session.as_mut().ok_or(LlmFacadeError::Protocol)?;
            let candidates = session.propose(version, budget.min(remaining)).await?;
            target.verify(version, &candidates, &draft).await?;
        }
    }))
}

/// Adapts the frontend's normalized generation request to DT without dropping sampling intent.
///
/// Runtime callers receive the existing token-stream contract, including one-time prompt metadata
/// and the original request identity. Unsupported options fail before either role is admitted.
/// Text decoding and stop-string handling remain with the frontend's existing output processor.
pub async fn generate_draft_target_tokens(
    draft: RoleClient,
    target: RoleClient,
    request: vllm_llm::GenerateRequest,
) -> Result<foretoken_llm_facade::TokenStream, LlmFacadeError> {
    use foretoken_engine_core_client::protocol::output::StopReason;
    use foretoken_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
    use foretoken_llm_facade::draft_target::TargetStopReason;
    use futures::StreamExt;
    use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo};

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
    let mut stream = generate_draft_target(
        draft,
        target,
        DraftTargetRequest {
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
        },
    )
    .await?;
    let mut prompt_info = Some(GeneratePromptInfo {
        prompt_token_ids: request.prompt_token_ids.into(),
        prompt_logprobs: None,
    });
    Ok(Box::pin(async_stream::try_stream! {
        while let Some(commit) = stream.next().await {
            let commit = commit?;
            let finish_reason = match (commit.finished, commit.finish_reason.as_deref()) {
                (false, None) => None,
                (true, Some("length")) => Some(FinishReason::Length),
                (true, Some("stop")) => Some(FinishReason::Stop(match commit.stop_reason {
                    Some(TargetStopReason::TokenId(id)) if Some(id) == eos => None,
                    Some(TargetStopReason::TokenId(id)) => Some(StopReason::TokenId(id)),
                    Some(TargetStopReason::Text(text)) => Some(StopReason::Text(text)),
                    None => None,
                })),
                _ => Err(LlmFacadeError::Protocol)?,
            };
            yield GenerateOutput {
                request_id: request.request_id.clone(),
                prompt_info: prompt_info.take(),
                token_ids: commit.token_ids,
                logprobs: None,
                finish_reason,
                cached_token_count: commit.cached_token_count,
                kv_transfer_params: None,
                ec_transfer_params: None,
            };
        }
    }))
}
