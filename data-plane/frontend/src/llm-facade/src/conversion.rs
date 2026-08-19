// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM adapter conversions between vLLM engine-native types and the Foretoken
//! neutral protocol types.
//!
//! This is the frontend-side mirror of the model-server conversion module: it
//! encodes a vLLM [`GenerateRequest`] into the neutral [`GenerateInput`] and
//! reconstructs a vLLM [`GenerateOutput`] from the neutral [`TokenOutput`].

use std::collections::BTreeMap;

use foretoken_model_protocol::{
    EngineExtensions, FinishReason, GenerateInput, Logprobs, SamplingParams, StopReason,
    TokenOutput,
};
use vllm_llm::{GenerateOutput, GeneratePromptInfo, GenerateRequest};

use crate::LlmFacadeError;

/// Keys used to carry engine-specific sampling fields through the neutral
/// `SamplingParams::extra_args` map. They mirror the corresponding
/// `EngineCoreSamplingParams` field names so the two adapters stay symmetric.
const EXTRA_THINKING_TOKEN_BUDGET: &str = "thinking_token_budget";
const EXTRA_REPETITION_DETECTION: &str = "repetition_detection";
const EXTRA_EOS_TOKEN_ID: &str = "eos_token_id";
const EXTRA_ALL_STOP_TOKEN_IDS: &str = "all_stop_token_ids";
const EXTRA_LOGIT_BIAS: &str = "logit_bias";
const EXTRA_ALLOWED_TOKEN_IDS: &str = "allowed_token_ids";
const EXTRA_BAD_WORDS_TOKEN_IDS: &str = "bad_words_token_ids";
const EXTRA_STRUCTURED_OUTPUTS: &str = "structured_outputs";
const EXTRA_LOGPROB_TOKEN_IDS: &str = "logprob_token_ids";
const EXTRA_SKIP_READING_PREFIX_CACHE: &str = "skip_reading_prefix_cache";

/// Encodes a vLLM [`GenerateRequest`] into the neutral [`GenerateInput`].
pub fn to_generate_input(request: GenerateRequest) -> Result<GenerateInput, LlmFacadeError> {
    let extensions = encode_extensions(
        request.mm_features,
        request.lora_request,
        request.reasoning_parser_kwargs,
    )?;
    Ok(GenerateInput {
        request_id: request.request_id,
        prompt_token_ids: request.prompt_token_ids,
        sampling_params: to_neutral_sampling(request.sampling_params)?,
        extensions: (extensions != EngineExtensions::default()).then_some(extensions),
        arrival_time: request.arrival_time,
        cache_salt: request.cache_salt,
        trace_headers: request.trace_headers,
        priority: request.priority,
        data_parallel_rank: request.data_parallel_rank,
        session_id: request.session_id,
    })
}

/// Reconstructs a vLLM [`GenerateOutput`] from the neutral [`TokenOutput`].
pub fn to_generate_output(output: TokenOutput) -> GenerateOutput {
    GenerateOutput {
        request_id: output.request_id,
        prompt_info: output
            .prompt_token_ids
            .map(|prompt_token_ids| GeneratePromptInfo {
                prompt_token_ids: prompt_token_ids.into(),
                prompt_logprobs: output.prompt_logprobs.map(to_vllm_logprobs),
            }),
        token_ids: output.token_ids,
        logprobs: output.logprobs.map(to_vllm_logprobs),
        finish_reason: output.finish_reason.map(to_vllm_finish_reason),
        cached_token_count: output.cached_token_count,
        kv_transfer_params: output.kv_transfer_params,
        ec_transfer_params: output.ec_transfer_params,
    }
}

fn to_neutral_sampling(
    params: vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams,
) -> Result<SamplingParams, LlmFacadeError> {
    let mut extra: BTreeMap<String, serde_json::Value> =
        params.extra_args.unwrap_or_default().into_iter().collect();

    insert_optional(
        &mut extra,
        EXTRA_THINKING_TOKEN_BUDGET,
        params.thinking_token_budget,
    )?;
    insert_optional(
        &mut extra,
        EXTRA_REPETITION_DETECTION,
        params.repetition_detection,
    )?;
    insert_optional(&mut extra, EXTRA_EOS_TOKEN_ID, params.eos_token_id)?;
    if !params.all_stop_token_ids.is_empty() {
        extra.insert(
            EXTRA_ALL_STOP_TOKEN_IDS.into(),
            serde_json::to_value(params.all_stop_token_ids)
                .map_err(|_| LlmFacadeError::RequestFailed)?,
        );
    }
    insert_optional(&mut extra, EXTRA_LOGIT_BIAS, params.logit_bias)?;
    insert_optional(
        &mut extra,
        EXTRA_ALLOWED_TOKEN_IDS,
        params.allowed_token_ids,
    )?;
    insert_optional(
        &mut extra,
        EXTRA_BAD_WORDS_TOKEN_IDS,
        params.bad_words_token_ids,
    )?;
    insert_optional(
        &mut extra,
        EXTRA_STRUCTURED_OUTPUTS,
        params.structured_outputs,
    )?;
    insert_optional(
        &mut extra,
        EXTRA_LOGPROB_TOKEN_IDS,
        params.logprob_token_ids,
    )?;
    insert_optional(
        &mut extra,
        EXTRA_SKIP_READING_PREFIX_CACHE,
        params.skip_reading_prefix_cache,
    )?;

    Ok(SamplingParams {
        temperature: params.temperature,
        top_p: params.top_p,
        top_k: params.top_k,
        seed: params.seed,
        max_tokens: params.max_tokens,
        min_tokens: params.min_tokens,
        min_p: params.min_p,
        frequency_penalty: params.frequency_penalty,
        presence_penalty: params.presence_penalty,
        repetition_penalty: params.repetition_penalty,
        logprobs: params.logprobs,
        prompt_logprobs: params.prompt_logprobs,
        stop_token_ids: params.stop_token_ids,
        extra_args: extra,
    })
}

fn encode_extensions(
    mm_features: Option<vllm_engine_core_client::protocol::multimodal::MmFeatures>,
    lora_request: Option<vllm_engine_core_client::protocol::lora::LoraRequest>,
    reasoning_parser_kwargs: Option<
        vllm_engine_core_client::protocol::request::ReasoningParserKwargs,
    >,
) -> Result<EngineExtensions, LlmFacadeError> {
    Ok(EngineExtensions {
        mm_features: encode_optional(mm_features)?,
        lora_request: encode_optional(lora_request)?,
        reasoning_parser_kwargs: encode_optional(reasoning_parser_kwargs)?,
    })
}

fn encode_optional<T: serde::Serialize>(
    value: Option<T>,
) -> Result<Option<rmpv::Value>, LlmFacadeError> {
    value
        .map(|value| rmpv::ext::to_value(value).map_err(|_| LlmFacadeError::RequestFailed))
        .transpose()
}

fn insert_optional<T: serde::Serialize>(
    extra: &mut BTreeMap<String, serde_json::Value>,
    key: &str,
    value: Option<T>,
) -> Result<(), LlmFacadeError> {
    if let Some(value) = value {
        extra.insert(
            key.into(),
            serde_json::to_value(value).map_err(|_| LlmFacadeError::RequestFailed)?,
        );
    }
    Ok(())
}

fn to_vllm_finish_reason(reason: FinishReason) -> vllm_llm::FinishReason {
    match reason {
        FinishReason::Stop(reason) => vllm_llm::FinishReason::Stop(reason.map(to_vllm_stop_reason)),
        FinishReason::Length => vllm_llm::FinishReason::Length,
        FinishReason::Abort => vllm_llm::FinishReason::Abort,
        FinishReason::Error => vllm_llm::FinishReason::Error,
        FinishReason::Repetition(reason) => {
            vllm_llm::FinishReason::Repetition(reason.map(to_vllm_stop_reason))
        }
    }
}

fn to_vllm_stop_reason(
    reason: StopReason,
) -> vllm_engine_core_client::protocol::output::StopReason {
    match reason {
        StopReason::TokenId(id) => {
            vllm_engine_core_client::protocol::output::StopReason::TokenId(id)
        }
        StopReason::Text(text) => vllm_engine_core_client::protocol::output::StopReason::Text(text),
    }
}

fn to_vllm_logprobs(logprobs: Logprobs) -> vllm_engine_core_client::protocol::logprobs::Logprobs {
    vllm_engine_core_client::protocol::logprobs::Logprobs {
        positions: logprobs
            .positions
            .into_iter()
            .map(
                |position| vllm_engine_core_client::protocol::logprobs::PositionLogprobs {
                    entries: position
                        .entries
                        .into_iter()
                        .map(
                            |entry| vllm_engine_core_client::protocol::logprobs::TokenLogprob {
                                token_id: entry.token_id,
                                logprob: entry.logprob,
                                rank: entry.rank,
                            },
                        )
                        .collect(),
                },
            )
            .collect(),
    }
}
