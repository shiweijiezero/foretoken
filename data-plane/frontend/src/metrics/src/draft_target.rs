// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Bounded DT round aggregates; request/worker identities belong only in traces.

use std::sync::LazyLock;

use foretoken_model_protocol::DraftTargetRoundObservation;
use prometheus_client::{
    encoding::{EncodeLabelSet, text::encode_registry},
    metrics::{
        counter::Counter,
        family::Family,
        histogram::{Histogram, exponential_buckets},
    },
    registry::Registry,
};

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct Labels {
    model_name: String,
    phase: String,
    outcome: String,
    stage: &'static str,
}

struct Metrics {
    registry: Registry,
    rounds: Family<Labels, Counter>,
    admissions: Family<Labels, Counter>,
    tokens: Family<Labels, Counter>,
    seconds: Family<Labels, Histogram, fn() -> Histogram>,
    bytes: Family<Labels, Counter>,
}
static METRICS: LazyLock<Metrics> = LazyLock::new(|| {
    let mut registry = Registry::default();
    let rounds = Family::default();
    let admissions = Family::default();
    let tokens = Family::default();
    let seconds = Family::new_with_constructor(
        (|| Histogram::new(exponential_buckets(0.0001, 2.0, 20))) as fn() -> Histogram,
    );
    let bytes = Family::default();
    registry.register(
        "foretoken_dt_admissions",
        "Native ticket admissions and rejections, separate from token acceptance",
        admissions.clone(),
    );
    registry.register(
        "foretoken_dt_rounds",
        "Executed DT rounds by phase and outcome",
        rounds.clone(),
    );
    registry.register(
        "foretoken_dt_tokens",
        "Observed proposed, verified, accepted and committed tokens",
        tokens.clone(),
    );
    registry.register(
        "foretoken_dt_stage_seconds",
        "Observed wall durations; stages overlap and shared batch time is not additive",
        seconds.clone(),
    );
    registry.register(
        "foretoken_dt_transfer_bytes",
        "Completed proposal and feature reads, distinguished by stage",
        bytes.clone(),
    );
    Metrics {
        registry,
        rounds,
        admissions,
        tokens,
        seconds,
        bytes,
    }
});

/// Records a received Target round for frontend metrics and request tracing.
/// Missing stages remain absent; failed streams may have no final observation.
pub fn record_draft_target_round(model: &str, observation: &DraftTargetRoundObservation) {
    let labels = |stage| Labels {
        model_name: model.to_owned(),
        phase: observation.phase.clone(),
        outcome: observation.outcome.clone(),
        stage,
    };
    METRICS.rounds.get_or_create(&labels("round")).inc();
    if let Some(admitted) = observation.admitted {
        let mut admission_labels = labels("admission");
        admission_labels.outcome = if admitted { "admitted" } else { "rejected" }.to_owned();
        METRICS.admissions.get_or_create(&admission_labels).inc();
    }
    for (stage, value) in [
        ("round", Some(observation.elapsed_seconds)),
        ("draft", observation.draft_seconds),
        ("target", observation.target_seconds),
    ] {
        if let Some(value) = value {
            METRICS.seconds.get_or_create(&labels(stage)).observe(value);
        }
    }
    for (engine, queue, execution, wait) in [
        (
            &observation.draft,
            "draft_queue",
            "draft_scheduled",
            "draft_remote_wait",
        ),
        (
            &observation.target,
            "target_queue",
            "target_scheduled",
            "target_remote_wait",
        ),
    ] {
        if let Some(engine) = engine {
            for (stage, value) in [
                (queue, engine.queue_seconds),
                (execution, engine.scheduled_seconds),
                (wait, engine.remote_wait_seconds),
            ] {
                METRICS.seconds.get_or_create(&labels(stage)).observe(value);
            }
        }
    }
    for (stage, count) in [
        ("proposed", observation.proposed_tokens.map(|v| v as u64)),
        (
            "verified",
            observation.target.as_ref().map(|v| v.verified_tokens),
        ),
        (
            "accepted",
            observation.target.as_ref().map(|v| v.accepted_tokens),
        ),
        ("committed", observation.committed_tokens.map(|v| v as u64)),
    ] {
        if let Some(count) = count {
            METRICS.tokens.get_or_create(&labels(stage)).inc_by(count);
        }
    }
    for (transfer, stage, read, prepare) in [
        (
            &observation.transfer,
            "transfer",
            "transfer_read",
            "transfer_prepare",
        ),
        (
            &observation.context_transfer,
            "context_transfer",
            "context_read",
            "context_prepare",
        ),
    ] {
        let Some(transfer) = transfer else { continue };
        METRICS
            .bytes
            .get_or_create(&labels(stage))
            .inc_by(transfer.bytes);
        METRICS
            .seconds
            .get_or_create(&labels(read))
            .observe(transfer.read_seconds);
        METRICS
            .seconds
            .get_or_create(&labels(prepare))
            .observe(transfer.prepare_seconds);
        let Some(detail) = &transfer.breakdown else {
            continue;
        };
        // Parent durations contain their nested phases. These histograms locate
        // waits; adding them would double-count asynchronous reads and RPCs.
        for (proposal_label, context_label, value) in [
            (
                "transfer_total",
                "context_total",
                Some(detail.total_seconds),
            ),
            ("transfer_export", "context_export", detail.export_seconds),
            (
                "transfer_claim",
                "context_claim",
                Some(detail.claim_seconds),
            ),
            (
                "transfer_validation",
                "context_validation",
                Some(detail.validation_seconds),
            ),
            (
                "transfer_receive",
                "context_receive",
                Some(detail.receive_seconds),
            ),
            (
                "transfer_poll",
                "context_poll",
                Some(detail.receive_poll_seconds),
            ),
            (
                "transfer_io_queue",
                "context_io_queue",
                Some(detail.io_queue_seconds),
            ),
            (
                "transfer_buffer",
                "context_buffer",
                Some(detail.buffer_seconds),
            ),
            (
                "transfer_read_fence",
                "context_read_fence",
                Some(detail.read_fence_seconds),
            ),
            (
                "transfer_backend_read",
                "context_backend_read",
                Some(detail.transport_read_seconds),
            ),
            ("transfer_ack", "context_ack", detail.ack_seconds),
            (
                "transfer_release",
                "context_release",
                detail.release_seconds,
            ),
        ] {
            if let Some(value) = value {
                let name = if stage == "transfer" {
                    proposal_label
                } else {
                    context_label
                };
                METRICS.seconds.get_or_create(&labels(name)).observe(value);
            }
        }
        for (rpc, proposal_labels, context_labels) in [
            (
                detail.claim_rpc.as_ref(),
                [
                    "transfer_claim_dispatch",
                    "transfer_claim_worker",
                    "transfer_claim_return",
                ],
                [
                    "context_claim_dispatch",
                    "context_claim_worker",
                    "context_claim_return",
                ],
            ),
            (
                Some(&detail.receive_rpc),
                [
                    "transfer_receive_dispatch",
                    "transfer_receive_worker",
                    "transfer_receive_return",
                ],
                [
                    "context_receive_dispatch",
                    "context_receive_worker",
                    "context_receive_return",
                ],
            ),
            (
                detail.ack_rpc.as_ref(),
                [
                    "transfer_ack_dispatch",
                    "transfer_ack_worker",
                    "transfer_ack_return",
                ],
                [
                    "context_ack_dispatch",
                    "context_ack_worker",
                    "context_ack_return",
                ],
            ),
            (
                detail.stage_rpc.as_ref(),
                [
                    "transfer_stage_dispatch",
                    "transfer_stage_worker",
                    "transfer_stage_return",
                ],
                [
                    "context_stage_dispatch",
                    "context_stage_worker",
                    "context_stage_return",
                ],
            ),
        ] {
            if let Some(rpc) = rpc {
                let names = if stage == "transfer" {
                    proposal_labels
                } else {
                    context_labels
                };
                for (name, value) in names.into_iter().zip([
                    rpc.dispatch_seconds,
                    rpc.worker_seconds,
                    rpc.return_seconds,
                ]) {
                    METRICS.seconds.get_or_create(&labels(name)).observe(value);
                }
            }
        }
    }
    tracing::info!(target: "foretoken::dt", request_id = %observation.request_id,
        round = observation.round, observation = ?observation, "DT round");
}

/// Encodes DT families into the scrape owner's OpenMetrics document without a second EOF.
pub(super) fn encode_metrics(body: &mut String) -> Result<(), std::fmt::Error> {
    encode_registry(body, &METRICS.registry)
}
