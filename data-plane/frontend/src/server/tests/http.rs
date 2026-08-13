// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Integration tests for the HTTP frontend, backed by a mock model-server.

use std::net::SocketAddr;
use std::sync::Arc;

use foretoken_chat::{CharTokenizer, ChatFacade};
use foretoken_model_server::{MockModelServer, build_mock_router};
use foretoken_server::{AppState, serve_listener};
use tokio::net::TcpListener;
use tokio::sync::oneshot;

/// Spawn a mock model-server plus the frontend, returning the frontend address,
/// a shutdown handle, and the mock's state (to assert what it received).
async fn spawn() -> (SocketAddr, oneshot::Sender<()>, Arc<MockModelServer>) {
    // Mock model-server on an ephemeral port.
    let mock_state = Arc::new(MockModelServer::default());
    let mock_router = build_mock_router(mock_state.clone());
    let mock_listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind mock port");
    let mock_addr = mock_listener.local_addr().expect("mock address");
    tokio::spawn(async move {
        axum::serve(mock_listener, mock_router).await.expect("mock");
    });

    // Chat facade: real DeepSeek V4 renderer + placeholder char tokenizer.
    let chat = Arc::new(ChatFacade::new(
        Arc::new(foretoken_chat::vllm::DeepSeekV4Renderer),
        Arc::new(CharTokenizer),
    ));
    let state = AppState {
        chat,
        model_server_url: format!("http://{mock_addr}"),
    };

    // Frontend on an ephemeral port.
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind frontend port");
    let addr = listener.local_addr().expect("frontend address");
    let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();
    tokio::spawn(async move {
        let _ = serve_listener(listener, state, async {
            let _ = shutdown_rx.await;
        })
        .await;
    });

    (addr, shutdown_tx, mock_state)
}

fn contains_subsequence(haystack: &[u32], needle: &[u32]) -> bool {
    haystack.windows(needle.len()).any(|w| w == needle)
}

#[tokio::test]
async fn health_returns_ok() {
    let (addr, shutdown, _) = spawn().await;
    let resp = reqwest::get(format!("http://{addr}/health"))
        .await
        .expect("health request");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let _ = shutdown.send(());
}

#[tokio::test]
async fn chat_completions_non_stream_returns_completion() {
    let (addr, shutdown, _) = spawn().await;
    let resp = reqwest::Client::new()
        .post(format!("http://{addr}/v1/chat/completions"))
        .json(&serde_json::json!({
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .send()
        .await
        .expect("completions request");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    let body: serde_json::Value = resp.json().await.expect("json body");
    assert_eq!(body["object"], "chat.completion");
    assert_eq!(body["model"], "test-model");
    assert_eq!(body["choices"][0]["finish_reason"], "stop");
    assert_eq!(body["choices"][0]["message"]["role"], "assistant");
    // Content comes from the mock model-server's tokens, detokenized.
    assert_eq!(body["choices"][0]["message"]["content"], "Hello world!");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn chat_completions_stream_returns_sse_chunks() {
    let (addr, shutdown, _) = spawn().await;
    let resp = reqwest::Client::new()
        .post(format!("http://{addr}/v1/chat/completions"))
        .json(&serde_json::json!({
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": true
        }))
        .send()
        .await
        .expect("stream request");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    assert_eq!(
        resp.headers()
            .get(reqwest::header::CONTENT_TYPE)
            .and_then(|v| v.to_str().ok()),
        Some("text/event-stream")
    );

    let text = resp.text().await.expect("sse body");
    let chunks: Vec<serde_json::Value> = text
        .split("\n\n")
        .filter_map(|line| line.strip_prefix("data: "))
        .map(|data| serde_json::from_str(data).expect("chunk json"))
        .collect();

    assert_eq!(chunks.len(), 2);

    // First chunk carries the role and the first content delta.
    assert_eq!(chunks[0]["choices"][0]["delta"]["role"], "assistant");
    assert_eq!(chunks[0]["choices"][0]["delta"]["content"], "Hello");
    assert!(chunks[0]["choices"][0]["finish_reason"].is_null());

    // Content deltas concatenate to the full mock reply.
    let content: String = chunks
        .iter()
        .map(|c| c["choices"][0]["delta"]["content"].as_str().unwrap_or(""))
        .collect();
    assert_eq!(content, "Hello world!");

    // Only the last chunk carries finish_reason.
    assert_eq!(chunks[1]["choices"][0]["finish_reason"], "stop");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn chat_completions_tokenizes_the_request() {
    let (addr, shutdown, mock_state) = spawn().await;
    let resp = reqwest::Client::new()
        .post(format!("http://{addr}/v1/chat/completions"))
        .json(&serde_json::json!({
            "model": "test-model",
            "messages": [{"role": "user", "content": "hi"}]
        }))
        .send()
        .await
        .expect("completions request");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);

    // The mock model-server received the request's tokenized prompt — the real
    // render + tokenize output, not a hardcoded token sequence.
    let received = mock_state.received_prompt_token_ids.lock().unwrap();
    assert_eq!(received.len(), 1);
    let ids = &received[0];
    assert!(!ids.is_empty(), "prompt should tokenize to non-empty ids");
    // The input "hi" (U+0068 U+0069) must appear verbatim in the prompt.
    assert!(
        contains_subsequence(ids, &[104, 105]),
        "token ids should contain the input 'hi', got {ids:?}"
    );

    let _ = shutdown.send(());
}
