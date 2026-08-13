// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Verifies the mock model-server helper itself.

mod common;

use std::sync::Arc;

use foretoken_model_protocol::{GenerateInput, RouteStage, TokenEvent};
use vllm_llm::FinishReason;

#[tokio::test]
async fn mock_receives_input_and_streams_fixed_tokens() {
    let state = Arc::new(common::MockModelServer::default());
    let router = common::build_mock_router(state.clone());

    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, router).await.unwrap();
    });

    let input = GenerateInput {
        stage: RouteStage::Aggregate,
        request_id: "req-1".to_string(),
        prompt_token_ids: vec![1, 2, 3],
        sampling_params: Default::default(),
        mm_features: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        reasoning_parser_kwargs: None,
        lora_request: None,
        session_id: None,
    };

    let response = reqwest::Client::new()
        .post(format!("http://{addr}/generate"))
        .json(&input)
        .send()
        .await
        .unwrap();

    assert_eq!(response.status(), reqwest::StatusCode::OK);

    let body = response.text().await.unwrap();
    let events: Vec<TokenEvent> = body
        .lines()
        .filter(|line| line.starts_with("data: "))
        .map(|line| serde_json::from_str(&line["data: ".len()..]).unwrap())
        .collect();

    assert_eq!(events.len(), 2);
    let TokenEvent::Token(first) = &events[0] else {
        panic!("expected token event");
    };
    assert_eq!(first.request_id, "req-1");
    assert_eq!(first.token_ids, vec![100, 200]);
    assert!(first.finish_reason.is_none());

    let TokenEvent::Token(second) = &events[1] else {
        panic!("expected token event");
    };
    assert_eq!(second.token_ids, vec![300]);
    assert_eq!(second.finish_reason, Some(FinishReason::Stop(None)));

    let received = state.received_prompt_token_ids.lock().unwrap();
    assert_eq!(received.as_slice(), &[vec![1, 2, 3]]);
}
