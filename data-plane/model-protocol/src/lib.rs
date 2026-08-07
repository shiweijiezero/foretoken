// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Versioned internal HTTP contract for already-tokenized vLLM requests.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use vllm_engine_core_client::protocol::dtype::ModelDtype;
use vllm_engine_core_client::protocol::logprobs::Logprobs;
use vllm_engine_core_client::protocol::lora::LoraRequest;
use vllm_engine_core_client::protocol::multimodal::MmFeatures;
use vllm_engine_core_client::protocol::request::ReasoningParserKwargs;
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo, GenerateRequest};

/// Request accepted by one group-local model-server after frontend preparation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GenerateInput {
    pub request_id: String,
    pub prompt_token_ids: Vec<u32>,
    pub sampling_params: EngineCoreSamplingParams,
    #[serde(default)]
    pub mm_features: Option<MmFeatures>,
    #[serde(default)]
    pub arrival_time: Option<f64>,
    #[serde(default)]
    pub cache_salt: Option<String>,
    #[serde(default)]
    pub trace_headers: Option<BTreeMap<String, String>>,
    #[serde(default)]
    pub priority: i32,
    #[serde(default)]
    pub data_parallel_rank: Option<u32>,
    #[serde(default)]
    pub reasoning_parser_kwargs: Option<ReasoningParserKwargs>,
    #[serde(default)]
    pub lora_request: Option<LoraRequest>,
}

impl From<GenerateRequest> for GenerateInput {
    fn from(request: GenerateRequest) -> Self {
        Self {
            request_id: request.request_id,
            prompt_token_ids: request.prompt_token_ids,
            sampling_params: request.sampling_params,
            mm_features: request.mm_features,
            arrival_time: request.arrival_time,
            cache_salt: request.cache_salt,
            trace_headers: request.trace_headers,
            priority: request.priority,
            data_parallel_rank: request.data_parallel_rank,
            reasoning_parser_kwargs: request.reasoning_parser_kwargs,
            lora_request: request.lora_request,
        }
    }
}

impl GenerateInput {
    /// Attach the connector-owned EC descriptor to a trusted prefill request.
    ///
    /// The descriptor remains opaque to Foretoken and is passed to vLLM through
    /// `SamplingParams.extra_args`, which is the upstream EC transfer boundary.
    /// A caller may not replace an existing descriptor.
    pub fn inject_ec_transfer_params(&mut self, params: serde_json::Value) -> Result<(), String> {
        let extra_args = self.sampling_params.extra_args.get_or_insert_default();
        if extra_args.contains_key("ec_transfer_params") {
            return Err("ec_transfer_params is already set".into());
        }
        extra_args.insert("ec_transfer_params".into(), params);
        Ok(())
    }
}

impl From<GenerateInput> for GenerateRequest {
    fn from(request: GenerateInput) -> Self {
        Self {
            request_id: request.request_id,
            prompt_token_ids: request.prompt_token_ids,
            sampling_params: request.sampling_params,
            mm_features: request.mm_features,
            arrival_time: request.arrival_time,
            cache_salt: request.cache_salt,
            trace_headers: request.trace_headers,
            priority: request.priority,
            data_parallel_rank: request.data_parallel_rank,
            reasoning_parser_kwargs: request.reasoning_parser_kwargs,
            lora_request: request.lora_request,
        }
    }
}

/// Explicitly scoped cancellation request. Empty lists are rejected by model-server.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AbortInput {
    #[serde(default)]
    pub request_ids: Vec<String>,
}

/// Canonical fields preserved from one vLLM output update.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TokenOutput {
    pub request_id: String,
    pub prompt_token_ids: Option<Vec<u32>>,
    pub prompt_logprobs: Option<Logprobs>,
    pub token_ids: Vec<u32>,
    pub logprobs: Option<Logprobs>,
    pub cached_token_count: usize,
    pub finish_reason: Option<FinishReason>,
    pub kv_transfer_params: Option<serde_json::Value>,
    pub ec_transfer_params: Option<serde_json::Value>,
}

impl From<GenerateOutput> for TokenOutput {
    fn from(output: GenerateOutput) -> Self {
        let (prompt_token_ids, prompt_logprobs) = match output.prompt_info {
            Some(prompt) => (
                Some(prompt.prompt_token_ids.to_vec()),
                prompt.prompt_logprobs,
            ),
            None => (None, None),
        };
        Self {
            request_id: output.request_id,
            prompt_token_ids,
            prompt_logprobs,
            token_ids: output.token_ids,
            logprobs: output.logprobs,
            cached_token_count: output.cached_token_count,
            finish_reason: output.finish_reason,
            kv_transfer_params: output.kv_transfer_params,
            ec_transfer_params: output.ec_transfer_params,
        }
    }
}

impl From<TokenOutput> for GenerateOutput {
    fn from(output: TokenOutput) -> Self {
        Self {
            request_id: output.request_id,
            prompt_info: output
                .prompt_token_ids
                .map(|prompt_token_ids| GeneratePromptInfo {
                    prompt_token_ids: prompt_token_ids.into(),
                    prompt_logprobs: output.prompt_logprobs,
                }),
            token_ids: output.token_ids,
            logprobs: output.logprobs,
            finish_reason: output.finish_reason,
            cached_token_count: output.cached_token_count,
            kv_transfer_params: output.kv_transfer_params,
            ec_transfer_params: output.ec_transfer_params,
        }
    }
}

/// The vLLM source revision compiled into Foretoken model-server images.
///
/// This pin matches the runtime image contract; it is distinct from the Python
/// package version reported by an EngineCore ready response.
pub const VLLM_PINNED_REVISION: &str = "5b14019576475224d86044b262e28a04a85d4086";

/// Model identity reported by a running model-server.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeModelIdentity {
    pub model: String,
    pub revision: String,
}

/// Controller-owned EC transfer settings observed by one model-server.
///
/// `fingerprint` identifies the transfer-compatible connector profile. It does
/// not include the producer/consumer role, which necessarily differs across an
/// encoder-prefill pair.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeEcTransferMetadata {
    pub role: String,
    pub profile: String,
    pub connector: String,
    pub fingerprint: String,
}

/// Versioned, observed model-server runtime metadata.
///
/// Capabilities are deliberately limited to facts the model-server can observe.
/// An empty set does not imply support for optional model features.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeMetadataResponse {
    pub version: u8,
    pub model: RuntimeModelIdentity,
    pub model_dtype: ModelDtype,
    pub effective_max_model_len: u32,
    pub vllm_pinned_revision: String,
    pub vllm_version: String,
    pub ec_transfer: Option<RuntimeEcTransferMetadata>,
    #[serde(default)]
    pub capabilities: std::collections::BTreeSet<String>,
}

/// Versioned capacity snapshot for trusted frontend routing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TelemetryResponse {
    pub version: u8,
    pub accepting: bool,
    pub running_requests: u64,
    pub max_concurrent_requests: u64,
}

/// Cursor parameters for the versioned KV delta endpoint.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KvDeltaQuery {
    pub epoch: Option<String>,
    pub after: Option<u64>,
    pub limit: Option<usize>,
}

/// KV-cache partition identity used by opaque delta events.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct KvPartition {
    pub scope_id: String,
    pub block_size: u32,
    pub group_idx: Option<u32>,
    pub spec_kind: String,
}

/// One KV mutation carried in a delta response.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum KvDeltaEvent {
    Store {
        partition: KvPartition,
        digest: String,
    },
    Remove {
        partition: KvPartition,
        digest: String,
    },
    Clear,
}

/// A sequenced KV mutation. The sequence is retained by consumers for cursor correctness.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KvDelta {
    pub sequence: u64,
    #[serde(flatten)]
    pub event: KvDeltaEvent,
}

/// One bounded page from a model-server KV delta stream.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KvDeltaResponse {
    pub backend_id: String,
    pub scope_id: String,
    pub epoch: String,
    pub dp_rank: u32,
    pub through: u64,
    pub current: u64,
    pub deltas: Vec<KvDelta>,
}

/// Stable terminal error categories for the internal token stream.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TokenErrorCode {
    Unavailable,
    RequestFailed,
    Protocol,
}

/// One NDJSON event returned by model-server.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum TokenEvent {
    Token(Box<TokenOutput>),
    Error {
        request_id: String,
        code: TokenErrorCode,
    },
}

#[cfg(test)]
#[path = "tests/protocol_conversion.rs"]
mod tests;
