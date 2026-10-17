// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral DT observations consumed by placement and round accounting.

use serde::{Deserialize, Serialize};
use std::num::NonZeroU32;

/// Version of the internal role protocol; frontend and both roles upgrade together.
pub const DRAFT_TARGET_PROTOCOL_VERSION: u32 = 9;

/// Concrete tensor path advertised by DT workers for compatible peer selection.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum DraftTargetTensorTransport {
    GpuIpc,
    Rdma,
    Zmq,
}

/// Worker-reported same-host GPU access; device identities are opaque to routing.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DraftTargetGpuIpc {
    pub host_id: String,
    pub device_id: String,
    pub peer_devices: Vec<String>,
}

impl DraftTargetGpuIpc {
    /// Checks both workers' granted peer access before Router selects a GPU IPC binding.
    pub fn supports_peer(&self, peer: &Self) -> bool {
        self.host_id == peer.host_id
            && self.peer_devices.contains(&peer.device_id)
            && peer.peer_devices.contains(&self.device_id)
    }
}

/// Candidate compatibility advertised by roles and consumed by request-level routing.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetCapabilities {
    pub candidate_format: crate::DraftTargetCandidateFormat,
    /// Initialized paths; empty for token-only roles. Target selects a common path at binding.
    pub tensor_transports: Vec<DraftTargetTensorTransport>,
    pub gpu_ipc: Option<DraftTargetGpuIpc>,
    pub context_profile: Option<serde_json::Value>,
    /// Target round policy; absent on Drafts. It is not engine capacity.
    pub default_draft_tokens: Option<NonZeroU32>,
    /// Target engine capacity or Draft method limit; None for ordinary autoregressive Drafts.
    pub max_candidate_tokens: Option<NonZeroU32>,
}

impl DraftTargetCapabilities {
    /// Validates role-specific policy/capacity metadata before registry publication.
    pub fn is_valid_for(&self, role: crate::ModelServerRole) -> bool {
        if self
            .tensor_transports
            .contains(&DraftTargetTensorTransport::GpuIpc)
            != self.gpu_ipc.is_some()
            || self.gpu_ipc.as_ref().is_some_and(|ipc| {
                ipc.host_id.is_empty() || ipc.device_id.is_empty() || ipc.peer_devices.is_empty()
            })
        {
            return false;
        }
        if !self.tensor_transports.is_empty()
            != matches!(
                self.candidate_format,
                crate::DraftTargetCandidateFormat::TokenIdsLogProbs
            )
        {
            return false;
        }
        match role {
            crate::ModelServerRole::Draft => self.default_draft_tokens.is_none(),
            crate::ModelServerRole::Target => matches!(
                (self.default_draft_tokens, self.max_candidate_tokens),
                (Some(default), Some(max)) if default <= max
            ),
            _ => false,
        }
    }

    /// Checks execution compatibility; Target bounds each round by both peers' capacities.
    pub fn supports_target(&self, target: &Self) -> bool {
        self.candidate_format == target.candidate_format
            && (self.tensor_transports.is_empty() && target.tensor_transports.is_empty()
                || self
                    .tensor_transports
                    .iter()
                    .any(|transport| {
                        target.tensor_transports.contains(transport)
                            && (*transport != DraftTargetTensorTransport::GpuIpc
                                || matches!((&self.gpu_ipc, &target.gpu_ipc), (Some(local), Some(peer)) if local.supports_peer(peer)))
                    }))
            && self.context_profile == target.context_profile
    }
}

/// One scheduler snapshot. Running includes remote waiters; ready excludes them.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetWorkload {
    pub collected_at_unix_ms: u64,
    pub running_requests: u64,
    pub ready_requests: u64,
    pub remote_waiting_requests: u64,
    pub waiting_requests: u64,
    /// Currently uncomputed known tokens, not predicted future generation.
    pub waiting_tokens: u64,
    pub ready_tokens: u64,
    pub max_num_seqs: u64,
    pub max_num_batched_tokens: u64,
    pub kv_cache_usage: f64,
    /// Allocatable device blocks, excluding the reserved null block.
    pub kv_capacity_blocks: u64,
    /// Pinned blocks; unreferenced prefix-cache blocks are reclaimable, hence excluded.
    pub kv_used_blocks: u64,
}

/// Allocated pages in one engine KV group; includes shared and speculative pages.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetKvGroup {
    pub block_tokens: u64,
    pub allocated_blocks: u64,
    pub allocated_bytes: u64,
}

/// A request's current native KV residency, not a migration payload or exclusive memory cost.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetKv {
    pub context_tokens: u64,
    pub computed_tokens: u64,
    pub groups: Vec<DraftTargetKvGroup>,
}

/// Engine measurements for one proposal or Target output interval, including native queueing.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetEngineObservation {
    /// Native steps consumed in this output interval, carrying logical execution identities.
    pub physical_batches: Vec<DraftTargetPhysicalBatch>,
    pub queue_seconds: f64,
    /// Sum of native schedule-to-result wall times, shared with other batch members; not GPU time.
    pub scheduled_seconds: f64,
    pub remote_wait_seconds: f64,
    pub scheduled_tokens: u64,
    pub verified_tokens: u64,
    /// Sampler acceptance before stop truncation, excluding correction/bonus tokens.
    pub accepted_tokens: u64,
    pub steps: u64,
    pub preemptions: u64,
    /// Actual final native step shape; a multi-step Draft can have different earlier shapes.
    pub batch_requests: u64,
    pub batch_tokens: u64,
    /// Residency immediately after native output; may precede frontend stop handling/abort.
    pub kv: Option<DraftTargetKv>,
}

/// Request-round identity attached to native work; slot and arrival order carry no identity.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct DraftTargetWorkIdentity {
    pub request_id: String,
    pub work_id: String,
    pub frontier: u64,
    pub round: u64,
    pub phase: String,
}

/// One native schedule/result interval, shared by every scheduled member.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetPhysicalBatch {
    pub batch_id: String,
    pub started_at_unix_ms: u64,
    pub finished_at_unix_ms: u64,
    pub scheduled_tokens: u64,
    pub scheduled_seconds: f64,
    pub members: Vec<DraftTargetPhysicalMember>,
}

/// Actual native token work, including prefill/replay and known request-round identity.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetPhysicalMember {
    pub engine_request_id: String,
    pub request_id: Option<String>,
    pub confirmed_frontier: Option<u64>,
    pub work: Option<DraftTargetWorkIdentity>,
    pub scheduled_tokens: u64,
}

/// Worker-read measurement and role-side preparation wall time for a proposal, feature prefix or KV checkpoint.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetTransferObservation {
    pub transport: DraftTargetTensorTransport,
    pub bytes: u64,
    /// Includes readiness fencing and transport polling; not wire-only latency.
    pub read_seconds: f64,
    /// Claim through read, ACK and staging, including control/Worker RPC overhead.
    pub prepare_seconds: f64,
}

/// One Target-coordinated round, attached to confirmed output for tracing and metrics.
/// Optional stages were not needed or unavailable; wall durations can overlap.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DraftTargetRoundObservation {
    pub request_id: String,
    pub round: u64,
    pub version: Option<u64>,
    pub phase: String,
    pub outcome: String,
    pub draft_endpoint: String,
    pub target_endpoint: String,
    pub tensor_transport: Option<DraftTargetTensorTransport>,
    pub context_tokens: usize,
    pub batch_requests: usize,
    pub elapsed_seconds: f64,
    /// Target-side wall time including Draft proposal HTTP overhead.
    pub draft_seconds: Option<f64>,
    /// Target preparation/admission through confirmed output and Draft synchronization.
    pub target_seconds: Option<f64>,
    pub proposed_tokens: Option<usize>,
    /// Target-selected candidate count, before Draft execution or native verification.
    pub requested_tokens: Option<usize>,
    pub committed_tokens: Option<usize>,
    /// Engine ticket admission; this is separate from candidate token acceptance.
    pub admitted: Option<bool>,
    pub draft: Option<DraftTargetEngineObservation>,
    pub target: Option<DraftTargetEngineObservation>,
    pub transfer: Option<DraftTargetTransferObservation>,
    /// Target-to-Draft feature preparation, reported by the consuming Draft.
    pub context_transfer: Option<DraftTargetTransferObservation>,
}

/// Token-input generation request; the caller owns tokenization and model compatibility.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftTargetRequest {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reservation: Option<foretoken_request_ledger::ReservationRef>,
    #[serde(default)]
    pub priority: i32,
    pub token_ids: Vec<u32>,
    pub max_tokens: u32,
    #[serde(default)]
    pub stop: Vec<String>,
    #[serde(default)]
    pub stop_token_ids: Vec<u32>,
    #[serde(default)]
    pub min_tokens: u32,
    #[serde(default)]
    pub ignore_eos: bool,
    #[serde(default)]
    pub sampling: DraftTargetSampling,
}

/// Explicit sampling choices understood by both independent roles.
#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct DraftTargetSampling {
    pub temperature: f32,
    pub top_p: f32,
    pub top_k: u32,
    pub seed: Option<i64>,
}

impl Default for DraftTargetSampling {
    fn default() -> Self {
        Self {
            temperature: 0.0,
            top_p: 1.0,
            top_k: 0,
            seed: None,
        }
    }
}

/// Confirmed Target output and the next engine generation, never a Draft guess.
#[derive(Debug, Serialize, Deserialize)]
pub struct TargetCommit {
    pub token_ids: Vec<u32>,
    pub version: Option<u64>,
    pub observation: crate::DraftTargetEngineObservation,
    pub transfer: Option<crate::DraftTargetTransferObservation>,
    pub finished: bool,
    pub finish_reason: Option<String>,
    pub stop_reason: Option<TargetStopReason>,
    pub cached_token_count: usize,
    /// Target-coordinator measurements; absent for manually driven role diagnostics.
    pub round: Option<crate::DraftTargetRoundObservation>,
}

/// The exact reason Target stopped, retained for the frontend's output processor.
#[derive(Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum TargetStopReason {
    TokenId(u32),
    Text(String),
}

/// A role's current admission and candidate capabilities, queried before binding.
#[derive(Clone, Deserialize)]
pub struct RoleStatus {
    #[serde(flatten)]
    pub candidates: DraftTargetCapabilities,
    pub protocol_version: u32,
    #[serde(default)]
    pub prepared_tokenizer: Option<crate::PreparedTokenizer>,
    pub role: String,
    pub model: String,
    pub revision: Option<String>,
    pub tokenizer: String,
    pub tokenizer_revision: Option<String>,
    pub max_model_len: u32,
    pub accepting: bool,
}
