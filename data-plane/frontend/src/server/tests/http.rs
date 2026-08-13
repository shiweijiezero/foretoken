// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Integration tests for the HTTP frontend.

use std::net::SocketAddr;

use foretoken_server::serve_listener;
use tokio::net::TcpListener;
use tokio::sync::oneshot;

/// Spawn the frontend on an ephemeral port and return its address plus a
/// shutdown handle.
async fn spawn() -> (SocketAddr, oneshot::Sender<()>) {
    let listener = TcpListener::bind("127.0.0.1:0")
        .await
        .expect("bind ephemeral port");
    let addr = listener.local_addr().expect("local address");
    let (shutdown_tx, shutdown_rx) = oneshot::channel::<()>();
    tokio::spawn(async move {
        let _ = serve_listener(listener, async {
            let _ = shutdown_rx.await;
        })
        .await;
    });
    (addr, shutdown_tx)
}

#[tokio::test]
async fn health_returns_ok() {
    let (addr, shutdown) = spawn().await;
    let resp = reqwest::get(format!("http://{addr}/health"))
        .await
        .expect("health request");
    assert_eq!(resp.status(), reqwest::StatusCode::OK);
    let _ = shutdown.send(());
}

#[tokio::test]
async fn chat_completions_non_stream_returns_completion() {
    let (addr, shutdown) = spawn().await;
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
    assert_eq!(body["choices"][0]["message"]["content"], "Hello world!");

    let _ = shutdown.send(());
}

#[tokio::test]
async fn chat_completions_stream_returns_sse_chunks() {
    let (addr, shutdown) = spawn().await;
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

    assert_eq!(chunks.len(), 3);

    // First chunk carries the role and the first content delta.
    assert_eq!(chunks[0]["choices"][0]["delta"]["role"], "assistant");
    assert_eq!(chunks[0]["choices"][0]["delta"]["content"], "Hello");
    assert!(chunks[0]["choices"][0]["finish_reason"].is_null());

    // Later chunks drop the role.
    assert!(chunks[1]["choices"][0]["delta"].get("role").is_none());

    // Content deltas concatenate to the full mock reply.
    let content: String = chunks
        .iter()
        .map(|c| c["choices"][0]["delta"]["content"].as_str().unwrap_or(""))
        .collect();
    assert_eq!(content, "Hello world!");

    // Only the last chunk carries finish_reason.
    assert_eq!(chunks[2]["choices"][0]["finish_reason"], "stop");

    let _ = shutdown.send(());
}
