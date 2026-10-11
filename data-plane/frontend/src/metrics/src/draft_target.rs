// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Bounded DT round aggregates; request/worker identities belong only in traces.

use std::sync::LazyLock;

use foretoken_model_protocol::DraftTargetRoundObservation;
use prometheus_client::{
    encoding::{EncodeLabelSet, text::encode},
    metrics::{
        counter::Counter,
        family::Family,
        histogram::{Histogram, exponential_buckets},
    },
    registry::Registry,
};

#[derive(Clone, Debug, Hash, PartialEq, Eq, EncodeLabelSet)]
struct Labels {
    phase: String,
    outcome: String,
    stage: &'static str,
}

struct Metrics {
    registry: Registry,
    rounds: Family<Labels, Counter>,
    tokens: Family<Labels, Counter>,
    seconds: Family<Labels, Histogram, fn() -> Histogram>,
    bytes: Family<Labels, Counter>,
}
static METRICS: LazyLock<Metrics> = LazyLock::new(|| {
    let mut registry = Registry::default();
    let rounds = Family::default();
    let tokens = Family::default();
    let seconds = Family::new_with_constructor(
        (|| Histogram::new(exponential_buckets(0.0001, 2.0, 20))) as fn() -> Histogram,
    );
    let bytes = Family::default();
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
        "Completed proposal or KV checkpoint reads observed at the frontend, by round phase",
        bytes.clone(),
    );
    Metrics {
        registry,
        rounds,
        tokens,
        seconds,
        bytes,
    }
});

/// Records a completed, cancelled or abandoned executor round for metrics and request tracing.
/// The round owner calls exactly once; incomplete stages remain absent from aggregates.
pub fn record_draft_target_round(observation: &DraftTargetRoundObservation) {
    let labels = |stage| Labels {
        phase: observation.phase.clone(),
        outcome: observation.outcome.clone(),
        stage,
    };
    METRICS.rounds.get_or_create(&labels("round")).inc();
    for (stage, value) in [
        ("round", Some(observation.elapsed_seconds)),
        ("dispatch_wait", Some(observation.dispatch_wait_seconds)),
        ("draft", observation.draft_seconds),
        ("proposal_wait", observation.proposal_wait_seconds),
        ("target", observation.target_seconds),
        (
            "kv_export",
            observation
                .migration
                .as_ref()
                .and_then(|m| m.export_seconds),
        ),
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
        (
            "checkpoint",
            observation
                .migration
                .as_ref()
                .and_then(|m| m.checkpoint_tokens)
                .map(|v| v as u64),
        ),
    ] {
        if let Some(count) = count {
            METRICS.tokens.get_or_create(&labels(stage)).inc_by(count);
        }
    }
    if let Some(transfer) = &observation.transfer {
        METRICS
            .bytes
            .get_or_create(&labels("transfer"))
            .inc_by(transfer.bytes);
        METRICS
            .seconds
            .get_or_create(&labels("transfer_read"))
            .observe(transfer.read_seconds);
        METRICS
            .seconds
            .get_or_create(&labels("transfer_prepare"))
            .observe(transfer.prepare_seconds);
    }
    tracing::info!(target: "foretoken::dt", request_id = %observation.request_id,
        round = observation.round, observation = ?observation, "DT round");
}

pub(super) fn render() -> Result<String, std::fmt::Error> {
    let mut body = String::new();
    encode(&mut body, &METRICS.registry)?;
    Ok(body)
}
