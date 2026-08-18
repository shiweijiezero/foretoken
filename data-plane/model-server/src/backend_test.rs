// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Instant;

use futures::{StreamExt, stream};
use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo};

use super::*;
use crate::backend_telemetry::BoundaryLatencyMetrics;

fn output(token_ids: Vec<u32>, finish_reason: Option<FinishReason>) -> GenerateOutput {
    GenerateOutput {
        request_id: "request".into(),
        prompt_info: Some(GeneratePromptInfo {
            prompt_token_ids: Arc::from([1]),
            prompt_logprobs: None,
        }),
        token_ids,
        logprobs: None,
        finish_reason,
        cached_token_count: 0,
        kv_transfer_params: None,
        ec_transfer_params: None,
    }
}

#[tokio::test]
async fn tracked_stream_releases_inflight_for_every_terminal_path() {
    let running = Arc::new(AtomicU64::new(0));
    let observations = Arc::new(Mutex::new(BoundaryLatencyMetrics::new()));
    let response = tracked_stream(
        stream::iter([Ok(output(vec![1], Some(FinishReason::stop_eos())))]),
        Instant::now(),
        running.clone(),
        observations.clone(),
    );
    tokio::task::yield_now().await;
    assert_eq!(running.load(Ordering::Acquire), 0);
    assert_eq!(observations.lock().unwrap().snapshot().2.count, 1);
    drop(response);

    let mut error = tracked_stream(
        stream::iter([Err(vllm_llm::Error::EngineCoreClient(
            vllm_engine_core_client::Error::RequestStreamClosed {
                request_id: "request".into(),
            },
        ))]),
        Instant::now(),
        running.clone(),
        observations.clone(),
    );
    assert!(error.next().await.unwrap().is_err());
    assert_eq!(running.load(Ordering::Acquire), 0);

    let mut dropped = tracked_stream(
        stream::iter([Ok(output(vec![1], None))]).chain(stream::pending()),
        Instant::now(),
        running.clone(),
        observations,
    );
    assert!(dropped.next().await.unwrap().is_ok());
    assert_eq!(running.load(Ordering::Acquire), 1);
    drop(dropped);
    tokio::task::yield_now().await;
    assert_eq!(running.load(Ordering::Acquire), 0);
}

#[tokio::test]
async fn tracked_stream_excludes_backpressure_and_unsuccessful_terminal_latency() {
    let running = Arc::new(AtomicU64::new(0));
    let observations = Arc::new(Mutex::new(BoundaryLatencyMetrics::new()));
    let mut backpressured = tracked_stream(
        stream::iter([
            Ok(output(vec![1], None)),
            Ok(output(vec![2], None)),
            Ok(output(vec![3], Some(FinishReason::stop_eos()))),
        ]),
        Instant::now(),
        running.clone(),
        observations.clone(),
    );
    tokio::task::yield_now().await;
    tokio::task::yield_now().await;
    while backpressured.next().await.is_some() {}

    let mut aborted = tracked_stream(
        stream::iter([Ok(output(vec![4], Some(FinishReason::Abort)))]),
        Instant::now(),
        running,
        observations.clone(),
    );
    while aborted.next().await.is_some() {}

    let (ttft, tpot, e2e) = observations.lock().unwrap().snapshot();
    assert_eq!(ttft.count, 2);
    assert_eq!(tpot.count, 0);
    assert_eq!(e2e.count, 0);
}
