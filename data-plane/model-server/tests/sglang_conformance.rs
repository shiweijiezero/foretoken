// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! SGLang adapter contract tests using a mock loopback HTTP server.

use axum::{Json, Router, routing::post};
use foretoken_model_protocol::{FinishReason, GenerateInput, SamplingParams, TokenEvent};
use foretoken_model_server::engine::sglang::{SglangBackend, SglangLaunchPlan};
use foretoken_model_server::engine::{Engine, EngineKind};
use futures::StreamExt;
use serde_json::{Value, json};
use std::sync::{Arc, Mutex};
use tokio::net::TcpListener;

fn generate_input() -> GenerateInput {
    GenerateInput {
        request_id: "req".into(),
        prompt_token_ids: vec![1, 2],
        sampling_params: SamplingParams::default(),
        extensions: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        session_id: None,
    }
}

/// Builds a mock SGLang server that streams two token chunks then a terminal.
async fn spawn_mock_server() -> (String, Arc<Mutex<Value>>) {
    let last_request: Arc<Mutex<Value>> = Arc::new(Mutex::new(Value::Null));

    let seen = last_request.clone();
    let app = Router::new().route(
        "/generate",
        post(move |Json(body): Json<Value>| {
            let seen = seen.clone();
            async move {
                *seen.lock().unwrap() = body;
                // Two streamed NDJSON chunks: a token then a terminal.
                (
                    axum::http::StatusCode::OK,
                    [("content-type", "application/json")],
                    format!(
                        "{}\n{}\n",
                        json!({"output_ids": [42]}),
                        json!({"output_ids": [], "meta_info": {"finish_reason": "stop"}})
                    ),
                )
            }
        }),
    );

    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });
    (format!("http://{addr}"), last_request)
}

#[tokio::test]
async fn generate_streams_tokens_and_terminal() {
    let (endpoint, seen) = spawn_mock_server().await;
    let backend = SglangBackend::new(endpoint);

    let mut stream = backend.generate(generate_input()).await.unwrap();
    let mut events = Vec::new();
    while let Some(event) = stream.next().await {
        events.push(event.unwrap());
    }

    assert_eq!(events.len(), 2);
    match &events[0] {
        TokenEvent::Token(output) => {
            assert_eq!(output.token_ids, vec![42]);
            assert_eq!(output.finish_reason, None);
        }
        TokenEvent::Error { .. } => panic!("unexpected error"),
    }
    match &events[1] {
        TokenEvent::Token(output) => {
            assert_eq!(output.finish_reason, Some(FinishReason::Stop(None)));
        }
        TokenEvent::Error { .. } => panic!("unexpected error"),
    }

    // The mock saw the tokenized request.
    let body = seen.lock().unwrap();
    assert_eq!(body["input_ids"], json!([1, 2]));
    assert_eq!(body["stream"], json!(true));
}

#[tokio::test]
async fn generate_reports_backend_error_on_non_success() {
    let app = Router::new().route(
        "/generate",
        post(|| async { axum::http::StatusCode::SERVICE_UNAVAILABLE }),
    );
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        axum::serve(listener, app).await.unwrap();
    });

    let backend = SglangBackend::new(format!("http://{addr}"));
    let result = backend.generate(generate_input()).await;
    assert!(result.is_err());
}

#[tokio::test]
async fn default_capabilities_and_cleanup() {
    let backend = SglangBackend::new("http://127.0.0.1:1".to_owned());
    let capabilities = backend.capabilities();
    assert!(!capabilities.kv_event_sources);
    assert!(!capabilities.supports_pd);
    assert!(!capabilities.supports_ec);
    assert!(backend.cleanup().await.is_ok());
}

#[test]
fn launch_plan_parses_and_defaults() {
    let plan = SglangLaunchPlan::parse(
        r#"{"version":1,"model":"Qwen/Qwen3-0.6B","port":30000,"startupSeconds":120,"drainSeconds":30}"#,
    )
    .unwrap();
    assert_eq!(plan.kind, EngineKind::Sglang);
    assert_eq!(plan.tp, 1);
    assert_eq!(plan.dp, 1);
    assert!(
        plan.render_args()
            .unwrap()
            .contains(&"--model-path=Qwen/Qwen3-0.6B".to_string())
    );
}
