// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral DT observations consumed by placement and round accounting.

use serde::{Deserialize, Serialize};

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

/// Worker-read measurement and role-side preparation wall time for one proposal or KV checkpoint.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetTransferObservation {
    pub bytes: u64,
    /// Includes readiness fencing and transport polling; not wire-only latency.
    pub read_seconds: f64,
    /// Claim through read, ACK and staging, including control/Worker RPC overhead.
    pub prepare_seconds: f64,
}

/// Live per-session query. `None` means the engine currently owns no KV for this session.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct DraftTargetKvObservation {
    pub collected_at_unix_ms: u64,
    pub kv: Option<DraftTargetKv>,
}

/// Source and copy-stage measurements for a caller-selected Target migration.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DraftTargetMigrationObservation {
    pub source_target_endpoint: String,
    pub checkpoint_tokens: Option<usize>,
    /// Source snapshot copy and publication, including control/Worker RPCs.
    pub export_seconds: Option<f64>,
}

/// One executed round; migration rounds also report their source and exported checkpoint.
/// Optional stages have not completed (or were not needed); they are never measured zeros.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DraftTargetRoundObservation {
    pub request_id: String,
    pub round: u64,
    pub version: Option<u64>,
    pub phase: String,
    pub outcome: String,
    pub draft_endpoint: String,
    pub target_endpoint: String,
    pub context_tokens: usize,
    pub batch_requests: usize,
    /// Time since the state became schedulable, including caller queue and worker selection.
    pub dispatch_wait_seconds: f64,
    pub elapsed_seconds: f64,
    /// Frontend wall time including Draft binding/commit and proposal HTTP overhead.
    pub draft_seconds: Option<f64>,
    /// Proposal-ready to batch submission; waiting for sibling proposals, not engine queueing.
    pub proposal_wait_seconds: Option<f64>,
    /// Submission to Target output; migration includes export and destination admission.
    pub target_seconds: Option<f64>,
    pub proposed_tokens: Option<usize>,
    pub committed_tokens: Option<usize>,
    pub draft: Option<DraftTargetEngineObservation>,
    pub target: Option<DraftTargetEngineObservation>,
    pub transfer: Option<DraftTargetTransferObservation>,
    pub migration: Option<DraftTargetMigrationObservation>,
}
