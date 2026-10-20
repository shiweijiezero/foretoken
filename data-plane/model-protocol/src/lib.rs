// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Versioned internal HTTP contract for already-tokenized vLLM requests.

mod kv_hash;
pub use kv_hash::normalized_kv_block_hash;

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use vllm_engine_core_client::protocol::dtype::ModelDtype;
use vllm_engine_core_client::protocol::logprobs::Logprobs;
use vllm_engine_core_client::protocol::lora::LoraRequest;
use vllm_engine_core_client::protocol::multimodal::MmFeatures;
use vllm_engine_core_client::protocol::request::ReasoningParserKwargs;
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
use vllm_llm::{FinishReason, GenerateOutput, GenerateRequest};

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

/// Concrete P/D transport selected for a model service and consumed by engine adapters.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum KvConnector {
    #[serde(rename = "MooncakeConnector")]
    Mooncake,
    #[serde(rename = "NixlConnector")]
    Nixl,
}

impl KvConnector {
    /// Returns the upstream connector name used by the vLLM launcher.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Mooncake => "MooncakeConnector",
            Self::Nixl => "NixlConnector",
        }
    }
}

/// Runtime capability identifying native reuse of a retained device prefix.
pub const KV_PREFIX_REUSE_CAPABILITY: &str = "kv_prefix_reuse";

/// Effective P/D settings shared by engine launch and frontend runtime observation.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeKvTransferMetadata {
    pub connector: KvConnector,
    #[serde(default)]
    pub bidirectional_kv_xfer: bool,
}

/// A source-owned retained prefix selected by routing, not permission to read its blocks.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KvReuseSource {
    pub route_target_id: String,
    /// Physical endpoint observed with this instance; claims and cancellations bypass the Group Service.
    pub model_server_endpoint: String,
    pub dp_rank: u32,
    pub instance_id: String,
    pub offer_id: String,
    pub matched_tokens: usize,
}

/// Request accepted by the single model-server ingress owned by one routable ModelGroup.
/// Its Pod placement is a runtime detail and does not create another routing identity.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct GenerateInput {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reservation: Option<foretoken_request_ledger::ReservationRef>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub kv_reuse: Option<KvReuseSource>,
    pub request_id: String,
    pub prompt_token_ids: Vec<u32>,
    pub sampling_params: EngineCoreSamplingParams,
    pub mm_features: Option<MmFeatures>,
    pub arrival_time: Option<f64>,
    pub cache_salt: Option<String>,
    pub trace_headers: Option<BTreeMap<String, String>>,
    #[serde(default)]
    pub priority: i32,
    pub data_parallel_rank: Option<u32>,
    pub session_id: Option<String>,
    pub reasoning_parser_kwargs: Option<ReasoningParserKwargs>,
    pub lora_request: Option<LoraRequest>,
}
impl From<GenerateRequest> for GenerateInput {
    fn from(request: GenerateRequest) -> Self {
        Self {
            reservation: None,
            kv_reuse: None,
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
/// Concrete tokenizer snapshot selected during model acquisition.
/// File revisions let frontends without the shared claim fetch the exact metadata.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PreparedTokenizer {
    pub scope: String,
    pub model: String,
    pub revision: String,
    pub directory: String,
    pub cache_binding: Option<String>,
    pub files: std::collections::BTreeMap<String, String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RuntimeMetadataResponse {
    pub version: u8,
    pub model: RuntimeModelIdentity,
    #[serde(default)]
    pub model_dtype: Option<ModelDtype>,
    pub effective_max_model_len: u32,
    /// Engine-configured logprob limit; -1 permits the complete model vocabulary.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_logprobs: Option<i32>,
    pub ec_transfer: Option<RuntimeEcTransferMetadata>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub prepared_tokenizer: Option<PreparedTokenizer>,
    #[serde(default)]
    pub capabilities: std::collections::BTreeSet<String>,
}

impl RuntimeMetadataResponse {
    /// Resolves observed P/D settings for routing from the extensible capability set.
    pub fn kv_transfer(&self) -> Option<RuntimeKvTransferMetadata> {
        let connector = [KvConnector::Mooncake, KvConnector::Nixl]
            .into_iter()
            .find(|connector| self.capabilities.contains(connector.as_str()))?;
        Some(RuntimeKvTransferMetadata {
            connector,
            bidirectional_kv_xfer: self.capabilities.contains(KV_PREFIX_REUSE_CAPABILITY),
        })
    }
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
/// Current engine scheduler observations for one globally identified data-parallel rank.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DataParallelTelemetry {
    pub data_parallel_rank: u32,
    pub scheduler_running_requests: Option<u64>,
    pub scheduler_waiting_requests: Option<u64>,
    pub kv_cache_usage: Option<f64>,
}

/// Cumulative first moment and sample count read from an engine-owned histogram.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct HistogramMoments {
    pub sum: f64,
    pub count: u64,
}

/// Engine-owned request distributions used by request-cost routing.
#[derive(Debug, Clone, Copy, Default, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct RequestCostTelemetry {
    pub prompt_tokens: Option<HistogramMoments>,
    pub generation_tokens: Option<HistogramMoments>,
}

#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TelemetryResponse {
    #[serde(default)]
    pub request_cost: RequestCostTelemetry,
    pub version: u8,
    pub collected_at_unix_ms: u64,
    pub accepting: bool,
    /// Rank-local gauges from the same observation as the group totals.
    #[serde(default)]
    pub data_parallel_ranks: Vec<DataParallelTelemetry>,
    pub running_requests: u64,
    /// Sum of engine-reported scheduler capacities, or `None` when any capacity is unknown.
    pub max_concurrent_requests: Option<u64>,
    pub scheduler_running_requests: Option<u64>,
    pub scheduler_waiting_requests: Option<u64>,
    pub kv_cache_usage: Option<f64>,
    pub prompt_tokens_total: Option<u64>,
    pub generation_tokens_total: Option<u64>,
    pub ttft_seconds: CumulativeHistogram,
    pub tpot_seconds: CumulativeHistogram,
    pub e2e_seconds: CumulativeHistogram,
}

/// One EngineCore's scheduler token demand, distinct from allocated KV cache occupancy.
/// The model-server publishes complete rank coverage for token-load autoscaling consumers.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SchedulerLoad {
    pub data_parallel_rank: u32,
    pub queued_prefill_tokens: u64,
    pub scheduled_decode_kv_tokens: u64,
    pub queued_decode_kv_tokens: u64,
    pub context_length: u64,
    pub kv_capacity_tokens: u64,
}

/// Versioned scheduler-demand response consumed only by the control-plane collector.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SchedulerLoadResponse {
    pub version: u8,
    pub collected_at_unix_ms: u64,
    pub engines: Vec<SchedulerLoad>,
}

/// Group-local route used by the frontend KV indexer to consume normalized deltas.
pub const KV_INDEX_DELTA_PATH: &str = "/v1/internal/kv-index/delta";

/// Read-only engine observations for every DP rank of a routing candidate.
pub const KV_LOCAL_PREFIX_PATH: &str = "/v1/internal/kv-index/local-prefix";
/// Read-only shared-cache observations for a tokenized routing request.
pub const KV_SHARED_PREFIX_PATH: &str = "/v1/internal/kv-index/shared-prefix";

/// Source-owned Decode prefixes observed without acquiring a native read lease.
pub const KV_RETAINED_PREFIX_PATH: &str = "/v1/internal/kv-index/retained-prefix";
/// Atomically acquires one retained prefix for an accepted Prefill execution.
pub const KV_PREFIX_CLAIM_PATH: &str = "/v1/internal/kv-transfer/claim";
/// Revokes an unstarted read or observes native completion of an in-use lease.
pub const KV_PREFIX_CANCEL_PATH: &str = "/v1/internal/kv-transfer/cancel";

/// Existing KV observation deadline shared by HTTP callers and Pod-local connector queries.
pub const KV_OBSERVATION_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

/// Cache identity of a processed media item; observations never transfer its tensors.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvMultimodalIdentity {
    pub identifier: String,
    pub offset: usize,
    pub length: usize,
}

/// Native cache identity used for read-only local and connector observations.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvPrefixRequest {
    pub prompt_token_ids: Vec<u32>,
    pub dp_rank: u32,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_salt: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lora_name: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub lora_path: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub multimodal: Vec<KvMultimodalIdentity>,
}

/// One native request whose exact computed prefix remains retained by a Decode engine.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KvRetainedOffer {
    pub id: String,
    pub matched_tokens: usize,
}

/// Read-only retained-prefix observation from one native DP scheduler.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct KvRetainedRankPrefix {
    pub dp_rank: u32,
    pub instance_id: String,
    pub offer: Option<KvRetainedOffer>,
}

/// Source identity and native offers returned to the frontend's request-local KV reader.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvRetainedPrefixResponse {
    pub model_group_id: String,
    pub scope_id: String,
    pub model_server_endpoint: String,
    pub ranks: Vec<KvRetainedRankPrefix>,
}

/// Acquires a retained source for one already accepted Prefill execution.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KvPrefixClaim {
    pub source: KvReuseSource,
    pub consumer_id: String,
    pub prefix: KvPrefixRequest,
}

/// Granted means native source workers hold the blocks until read completion or revocation.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "status")]
pub enum KvPrefixClaimResult {
    Granted { descriptor: serde_json::Value },
    Miss,
}

/// Identifies a claim independently of whether its acknowledgement reached the receiver.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KvPrefixCancel {
    pub source: KvReuseSource,
    pub consumer_id: String,
}

/// Native cancellation result; in-use references remain held until transport completion.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(tag = "status")]
pub enum KvPrefixCancelResult {
    Released,
    InUse,
    Miss,
}

/// A source-confirmed reusable prefix, not resident block count or a cache lease.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvSharedPrefixResponse {
    pub model_group_id: String,
    pub scope_id: String,
    pub placement: KvPlacement,
    pub matched_tokens: usize,
}

/// Engine-confirmed local prefixes remain separate for every data-parallel rank.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvLocalPrefixResponse {
    pub model_group_id: String,
    pub scope_id: String,
    pub ranks: Vec<KvRankPrefix>,
}

/// Native query result for one scheduler; the engine owns group alignment and validity.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KvRankPrefix {
    pub dp_rank: u32,
    pub matched_tokens: usize,
    #[serde(default)]
    pub offloaded: Vec<KvTierPrefix>,
}

/// A connector-confirmed continuation, including any locally reusable prefix.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct KvTierPrefix {
    pub tier: KvStorageTier,
    pub matched_tokens: usize,
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

/// Model and layout namespace for normalized hashes. Cache salt enters the root block hash;
/// unsupported LoRA, multimodal, and unknown extra cache keys never produce token-only matches.
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
/// Engine-reported cache layout required to bind a locality query to its event partition.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct KvCacheGroup {
    pub group_idx: u32,
    pub spec_kind: String,
    pub sliding_window: Option<u32>,
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
    /// Absent when the producer cannot establish the engine's layout; consumers must not guess.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub cache_groups: Option<Vec<KvCacheGroup>>,
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
