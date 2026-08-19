// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM adapter conversions between Foretoken neutral protocol types and
//! vLLM engine-native types.
//!
//! The neutral `foretoken_model_protocol` types carry no engine dependency;
//! this module is the sole place where the neutral wire contract is translated
//! into and out of vLLM's native `GenerateRequest`/`GenerateOutput` and
//! `EngineCoreSamplingParams` representations.

use std::collections::{BTreeMap, BTreeSet};

use foretoken_model_protocol::{
    EngineExtensions, FinishReason, GenerateInput, Logprobs, ModelDtype, PositionLogprobs,
    SamplingParams, StopReason, TokenLogprob, TokenOutput,
};
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
use vllm_engine_core_client::protocol::{
    lora::LoraRequest, multimodal::MmFeatures, request::ReasoningParserKwargs,
};
use vllm_llm::{GenerateOutput, GenerateRequest};

use super::backend::VllmError;

/// Decoded engine-specific request extensions.
type DecodedExtensions = (
    Option<MmFeatures>,
    Option<ReasoningParserKwargs>,
    Option<LoraRequest>,
);

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

/// Translates a neutral [`GenerateInput`] into a vLLM [`GenerateRequest`].
///
/// Fails with [`VllmError::InvalidRequest`] when an opaque extension or
/// engine-specific sampling field cannot be decoded into its vLLM type.
pub fn to_vllm_request(input: GenerateInput) -> Result<GenerateRequest, VllmError> {
    let sampling_params = to_vllm_sampling(input.sampling_params)?;
    let (mm_features, reasoning_parser_kwargs, lora_request) = decode_extensions(input.extensions)?;

    Ok(GenerateRequest {
        request_id: input.request_id,
        prompt_token_ids: input.prompt_token_ids,
        sampling_params,
        mm_features,
        arrival_time: input.arrival_time,
        cache_salt: input.cache_salt,
        trace_headers: input.trace_headers,
        priority: input.priority,
        data_parallel_rank: input.data_parallel_rank,
        session_id: input.session_id,
        reasoning_parser_kwargs,
        lora_request,
    })
}

/// Translates a vLLM [`GenerateOutput`] into the neutral [`TokenOutput`].
pub fn to_token_output(output: GenerateOutput) -> TokenOutput {
    let (prompt_token_ids, prompt_logprobs) = match output.prompt_info {
        Some(prompt) => (
            Some(prompt.prompt_token_ids.to_vec()),
            prompt.prompt_logprobs.map(to_neutral_logprobs),
        ),
        None => (None, None),
    };
    TokenOutput {
        request_id: output.request_id,
        prompt_token_ids,
        prompt_logprobs,
        token_ids: output.token_ids,
        logprobs: output.logprobs.map(to_neutral_logprobs),
        cached_token_count: output.cached_token_count,
        finish_reason: output.finish_reason.map(to_neutral_finish_reason),
        kv_transfer_params: output.kv_transfer_params,
        ec_transfer_params: output.ec_transfer_params,
    }
}

fn to_vllm_sampling(params: SamplingParams) -> Result<EngineCoreSamplingParams, VllmError> {
    let mut extra = params.extra_args;

    let thinking_token_budget = take_optional(&mut extra, EXTRA_THINKING_TOKEN_BUDGET)?;
    let repetition_detection = take_optional(&mut extra, EXTRA_REPETITION_DETECTION)?;
    let eos_token_id = take_optional(&mut extra, EXTRA_EOS_TOKEN_ID)?;
    let all_stop_token_ids: BTreeSet<u32> =
        take_optional(&mut extra, EXTRA_ALL_STOP_TOKEN_IDS)?.unwrap_or_default();
    let logit_bias = take_optional(&mut extra, EXTRA_LOGIT_BIAS)?;
    let allowed_token_ids = take_optional(&mut extra, EXTRA_ALLOWED_TOKEN_IDS)?;
    let bad_words_token_ids = take_optional(&mut extra, EXTRA_BAD_WORDS_TOKEN_IDS)?;
    let structured_outputs = take_optional(&mut extra, EXTRA_STRUCTURED_OUTPUTS)?;
    let logprob_token_ids = take_optional(&mut extra, EXTRA_LOGPROB_TOKEN_IDS)?;
    let skip_reading_prefix_cache = take_optional(&mut extra, EXTRA_SKIP_READING_PREFIX_CACHE)?;

    let extra_args = if extra.is_empty() {
        None
    } else {
        Some(extra.into_iter().collect())
    };

    Ok(EngineCoreSamplingParams {
        temperature: params.temperature,
        top_p: params.top_p,
        top_k: params.top_k,
        seed: params.seed,
        max_tokens: params.max_tokens,
        min_tokens: params.min_tokens,
        thinking_token_budget,
        logprobs: params.logprobs,
        prompt_logprobs: params.prompt_logprobs,
        min_p: params.min_p,
        frequency_penalty: params.frequency_penalty,
        presence_penalty: params.presence_penalty,
        repetition_penalty: params.repetition_penalty,
        repetition_detection,
        stop_token_ids: params.stop_token_ids,
        eos_token_id,
        all_stop_token_ids,
        logit_bias,
        allowed_token_ids,
        bad_words_token_ids,
        structured_outputs,
        logprob_token_ids,
        skip_reading_prefix_cache,
        extra_args,
    })
}

fn decode_extensions(extensions: Option<EngineExtensions>) -> Result<DecodedExtensions, VllmError> {
    let Some(extensions) = extensions else {
        return Ok((None, None, None));
    };
    let mm_features = extensions
        .mm_features
        .map(rmpv::ext::from_value)
        .transpose()
        .map_err(|_| VllmError::InvalidRequest)?;
    let lora_request = extensions
        .lora_request
        .map(rmpv::ext::from_value)
        .transpose()
        .map_err(|_| VllmError::InvalidRequest)?;
    let reasoning_parser_kwargs = extensions
        .reasoning_parser_kwargs
        .map(rmpv::ext::from_value)
        .transpose()
        .map_err(|_| VllmError::InvalidRequest)?;
    Ok((mm_features, reasoning_parser_kwargs, lora_request))
}

fn take_optional<T: serde::de::DeserializeOwned>(
    extra: &mut BTreeMap<String, serde_json::Value>,
    key: &str,
) -> Result<Option<T>, VllmError> {
    match extra.remove(key) {
        Some(value) => serde_json::from_value(value)
            .map(Some)
            .map_err(|_| VllmError::InvalidRequest),
        None => Ok(None),
    }
}

fn to_neutral_finish_reason(reason: vllm_llm::FinishReason) -> FinishReason {
    match reason {
        vllm_llm::FinishReason::Stop(reason) => {
            FinishReason::Stop(reason.map(to_neutral_stop_reason))
        }
        vllm_llm::FinishReason::Length => FinishReason::Length,
        vllm_llm::FinishReason::Abort => FinishReason::Abort,
        vllm_llm::FinishReason::Error => FinishReason::Error,
        vllm_llm::FinishReason::Repetition(reason) => {
            FinishReason::Repetition(reason.map(to_neutral_stop_reason))
        }
    }
}

/// Translates a vLLM [`ModelDtype`] into the neutral [`ModelDtype`].
pub fn to_neutral_model_dtype(
    dtype: vllm_engine_core_client::protocol::dtype::ModelDtype,
) -> ModelDtype {
    match dtype {
        vllm_engine_core_client::protocol::dtype::ModelDtype::Float16 => ModelDtype::Float16,
        vllm_engine_core_client::protocol::dtype::ModelDtype::BFloat16 => ModelDtype::BFloat16,
        vllm_engine_core_client::protocol::dtype::ModelDtype::Float32 => ModelDtype::Float32,
    }
}

fn to_neutral_stop_reason(
    reason: vllm_engine_core_client::protocol::output::StopReason,
) -> StopReason {
    match reason {
        vllm_engine_core_client::protocol::output::StopReason::TokenId(id) => {
            StopReason::TokenId(id)
        }
        vllm_engine_core_client::protocol::output::StopReason::Text(text) => StopReason::Text(text),
    }
}

fn to_neutral_logprobs(
    logprobs: vllm_engine_core_client::protocol::logprobs::Logprobs,
) -> Logprobs {
    Logprobs {
        positions: logprobs
            .positions
            .into_iter()
            .map(|position| PositionLogprobs {
                entries: position
                    .entries
                    .into_iter()
                    .map(|entry| TokenLogprob {
                        token_id: entry.token_id,
                        logprob: entry.logprob,
                        rank: entry.rank,
                    })
                    .collect(),
            })
            .collect(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use vllm_llm::GeneratePromptInfo;

    #[test]
    fn sampling_params_round_trips_engine_specific_fields() {
        let mut extra = BTreeMap::new();
        extra.insert(EXTRA_THINKING_TOKEN_BUDGET.into(), serde_json::json!(128));
        let params = SamplingParams {
            stop_token_ids: vec![151643],
            extra_args: extra,
            ..Default::default()
        };
        let vllm = to_vllm_sampling(params).expect("decode sampling params");
        assert_eq!(vllm.thinking_token_budget, Some(128));
        assert_eq!(vllm.stop_token_ids, vec![151643]);
        assert!(vllm.extra_args.is_none());
    }

    #[test]
    fn sampling_params_rejects_malformed_engine_fields() {
        let mut extra = BTreeMap::new();
        extra.insert(EXTRA_LOGIT_BIAS.into(), serde_json::json!("not-a-map"));
        let params = SamplingParams {
            extra_args: extra,
            ..Default::default()
        };
        assert!(matches!(
            to_vllm_sampling(params),
            Err(VllmError::InvalidRequest)
        ));
    }

    #[test]
    fn finish_reason_converts() {
        assert_eq!(
            to_neutral_finish_reason(vllm_llm::FinishReason::Length),
            FinishReason::Length
        );
        assert_eq!(
            to_neutral_finish_reason(vllm_llm::FinishReason::Stop(Some(
                vllm_engine_core_client::protocol::output::StopReason::TokenId(7)
            ))),
            FinishReason::Stop(Some(StopReason::TokenId(7)))
        );
    }

    #[test]
    fn token_output_round_trips_prompt_info() {
        let output = GenerateOutput {
            request_id: "req".into(),
            prompt_info: Some(GeneratePromptInfo {
                prompt_token_ids: vec![1, 2, 3].into(),
                prompt_logprobs: None,
            }),
            token_ids: vec![4],
            logprobs: None,
            finish_reason: Some(vllm_llm::FinishReason::Stop(None)),
            cached_token_count: 2,
            kv_transfer_params: None,
            ec_transfer_params: None,
        };
        let token_output = to_token_output(output);
        assert_eq!(token_output.request_id, "req");
        assert_eq!(
            token_output.prompt_token_ids.as_deref(),
            Some(&[1, 2, 3][..])
        );
        assert_eq!(token_output.token_ids, vec![4]);
        assert_eq!(token_output.finish_reason, Some(FinishReason::Stop(None)));
    }
}
