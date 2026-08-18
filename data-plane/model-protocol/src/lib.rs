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

/// Execution responsibility of one routable ModelGroup.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ModelServerRole {
    #[default]
    Aggregate,
    Encoder,
    Prefill,
    Decode,
}

/// Model-server wire execution stage selected for a routed request.
///
/// This typed value is sent to the model-server HTTP endpoint. It is distinct from the Router's
/// request-local routing state and from a route target's execution role.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RouteStage {
    #[default]
    Aggregate,
    Encoder,
    Prefill,
    Decode,
}

/// Request accepted by the single model-server ingress owned by one routable ModelGroup.
/// Its Pod placement is a runtime detail and does not create another routing identity.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GenerateInput {
    /// Model-server execution stage selected by the router, not Router's per-request routing stage.
    #[serde(default)]
    pub stage: RouteStage,
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
    pub session_id: Option<String>,
    #[serde(default)]
    pub reasoning_parser_kwargs: Option<ReasoningParserKwargs>,
    #[serde(default)]
    pub lora_request: Option<LoraRequest>,
}
impl From<GenerateRequest> for GenerateInput {
    fn from(request: GenerateRequest) -> Self {
        Self {
            stage: RouteStage::Aggregate,
            request_id: request.request_id,
            prompt_token_ids: request.prompt_token_ids,
            sampling_params: request.sampling_params,
            mm_features: request.mm_features,
            arrival_time: request.arrival_time,
            cache_salt: request.cache_salt,
            trace_headers: request.trace_headers,
            priority: request.priority,
            data_parallel_rank: request.data_parallel_rank,
            session_id: request.session_id,
            reasoning_parser_kwargs: request.reasoning_parser_kwargs,
            lora_request: request.lora_request,
        }
    }
}
impl GenerateInput {
    /// Set the model-server stage after route selection.
    pub fn with_stage(mut self, stage: RouteStage) -> Self {
        self.stage = stage;
        self
    }

    /// Attach the connector-owned EC descriptor to a trusted prefill request.
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
            session_id: request.session_id,
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

/// Fields preserved from one vLLM output update.
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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeModelIdentity {
    pub model: String,
    pub revision: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeEcTransferMetadata {
    pub role: String,
    pub profile: String,
    pub connector: String,
}
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeMetadataResponse {
    pub version: u8,
    pub model: RuntimeModelIdentity,
    pub model_dtype: ModelDtype,
    pub effective_max_model_len: u32,
    pub ec_transfer: Option<RuntimeEcTransferMetadata>,
    #[serde(default)]
    pub capabilities: std::collections::BTreeSet<String>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CumulativeHistogramBucket {
    pub le_seconds: f64,
    pub count: u64,
}
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CumulativeHistogram {
    pub count: u64,
    pub sum_seconds: f64,
    pub buckets: Vec<CumulativeHistogramBucket>,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TelemetryResponse {
    pub version: u8,
    pub collected_at_unix_ms: u64,
    pub accepting: bool,
    pub running_requests: u64,
    pub max_concurrent_requests: u64,
    pub scheduler_running_requests: Option<u64>,
    pub scheduler_waiting_requests: Option<u64>,
    pub kv_cache_usage: Option<f64>,
    pub prompt_tokens_total: Option<u64>,
    pub generation_tokens_total: Option<u64>,
    pub ttft_seconds: CumulativeHistogram,
    pub tpot_seconds: CumulativeHistogram,
    pub e2e_seconds: CumulativeHistogram,
}

/// Cursor parameters for one source-local, zero-based delta stream. `None` means no event
/// has been consumed; an empty page never advances `after`.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvDeltaQuery {
    pub dp_rank: u32,
    pub epoch: Option<String>,
    pub after: Option<u64>,
    pub limit: Option<usize>,
}

/// Hash representation shared by request-side lookup and backend event adapters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KvHashFormat {
    NormalizedKeyedBlake3V1,
}
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(transparent)]
pub struct KvBlockHash(pub String);

/// Physical storage and its locality relative to the event source.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KvStorageTier {
    Device,
    HostPinned,
    Disk,
    External,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KvCacheLocality {
    /// Adapter could not establish locality; consumers must treat it as unavailable.
    Unspecified,
    Local,
    Remote,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvPlacement {
    pub tier: KvStorageTier,
    pub locality: KvCacheLocality,
}

/// Complete semantic namespace for request-side normalized hashes. A missing request discriminator
/// (LoRA, multimodal input, cache salt, or unknown extra cache key) is unsupported, never a miss.
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvPartition {
    pub model_revision: String,
    pub scope_id: String,
    pub hash_format: KvHashFormat,
    pub hash_block_size: u32,
    pub group_idx: Option<u32>,
    pub spec_kind: String,
    pub sliding_window: Option<u32>,
}

/// A normalized block produced from a backend-reported parent chain.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvStoredBlock {
    pub partition: KvPartition,
    pub block_index: u64,
    pub parent_hash: KvBlockHash,
    pub block_hash: KvBlockHash,
}

/// Backend-neutral KV block lifecycle events emitted by a model-server adapter.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(
    tag = "kind",
    rename_all = "snake_case",
    rename_all_fields = "camelCase",
    deny_unknown_fields
)]
pub enum KvDeltaEvent {
    BlockStored {
        blocks: Vec<KvStoredBlock>,
        placement: KvPlacement,
    },
    BlockRemoved {
        block_hashes: Vec<KvBlockHash>,
        placement: KvPlacement,
        group_idx: Option<u32>,
    },
    AllBlocksCleared,
}
/// One sequenced, nested KV lifecycle event. Nested encoding keeps both envelope and event payload
/// validation strict without serde's flattened internally-tagged-enum limitation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvDelta {
    pub sequence: u64,
    pub event: KvDeltaEvent,
}
/// Bounded source-local page. Identity, producer epoch and rank are independent from route ID.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvDeltaResponse {
    pub event_source_id: String,
    pub model_group_id: String,
    pub epoch: String,
    pub dp_rank: u32,
    pub through: u64,
    pub current: u64,
    pub deltas: Vec<KvDelta>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum TokenErrorCode {
    Unavailable,
    RequestFailed,
    Protocol,
}
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum TokenEvent {
    Token(Box<TokenOutput>),
    Error {
        request_id: String,
        code: TokenErrorCode,
    },
}
