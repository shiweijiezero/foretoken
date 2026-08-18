// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

mod http_support;

use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use foretoken_server::router;
use foretoken_text::Prompt;
use tower::ServiceExt;

use http_support::{RecordingGeneration, SuccessfulGeneration};

fn request(uri: &str, body: impl Into<Body>) -> Request<Body> {
    Request::builder()
        .method("POST")
        .uri(uri)
        .header("content-type", "application/json")
        .body(body.into())
        .unwrap()
}

#[tokio::test]
async fn openai_generation_routes_lower_requests_and_issue_server_request_ids() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(|| vec!["served-model".into()]),
        std::time::Duration::from_secs(1),
    );

    let completion = app
        .clone()
        .oneshot(request(
            "/v1/completions",
            r#"{"model":"served-model","prompt":[1,2,3],"stream":true,"top_k":20,"min_p":0.1,"seed":7,"max_tokens":64,"min_tokens":2,"stop_token_ids":[9],"ignore_eos":true,"logprobs":5,"prompt_logprobs":3,"priority":4,"cache_salt":"tenant","session_id":"session-1","request_id":"client-id"}"#,
        ))
        .await
        .unwrap();
    assert_eq!(completion.status(), StatusCode::BAD_GATEWAY);

    let chat = app
        .oneshot(request(
            "/v1/chat/completions",
            r#"{"model":"served-model","include_reasoning":true,"response_format":{"type":"json_schema","json_schema":{"name":"answer","schema":{"type":"object"}}},"messages":[{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"weather","arguments":"{}"}}]},{"role":"tool","tool_call_id":"call_1","content":"sunny"},{"role":"user","content":"continue"}],"request_id":"client-id"}"#,
        ))
        .await
        .unwrap();
    assert_eq!(chat.status(), StatusCode::BAD_GATEWAY);

    let captured = generation.text_requests.lock().unwrap();
    let (prompt, sampling, intermediate, priority, cache_salt, session_id, arrival_time) =
        &captured[0];
    assert_eq!(prompt, &Prompt::TokenIds(vec![1, 2, 3]));
    assert_eq!(sampling.top_k, Some(20));
    assert_eq!(sampling.min_p, Some(0.1));
    assert_eq!(sampling.seed, Some(7));
    assert_eq!(sampling.min_tokens, Some(2));
    assert_eq!(sampling.stop_token_ids, Some(vec![9]));
    assert!(sampling.ignore_eos);
    assert_eq!(sampling.logprobs, Some(5));
    assert_eq!(sampling.prompt_logprobs, Some(3));
    assert!(*intermediate);
    assert_eq!(*priority, 4);
    assert_eq!(cache_salt.as_deref(), Some("tenant"));
    assert_eq!(session_id.as_deref(), Some("session-1"));
    assert!(arrival_time.is_some());
    drop(captured);

    let captured = generation.chat_requests.lock().unwrap();
    assert_eq!(captured.len(), 1);
    assert!(captured[0].1);
    assert!(captured[0].0.sampling_params.structured_outputs.is_some());
    drop(captured);

    let request_ids = generation.request_ids.lock().unwrap();
    assert_eq!(request_ids.len(), 2);
    assert!(request_ids.iter().any(|id| id.starts_with("cmpl-")));
    assert!(request_ids.iter().any(|id| id.starts_with("chatcmpl-")));
    assert!(request_ids.iter().all(|id| id != "client-id"));
}

#[tokio::test]
async fn openai_http_surface_resolves_models_and_tokenization() {
    let generation = Arc::new(RecordingGeneration::default());
    let models = Arc::new(Mutex::new(vec!["first".to_owned()]));
    let source = models.clone();
    let app = router(
        generation,
        Arc::new(move || source.lock().unwrap().clone()),
        std::time::Duration::from_secs(1),
    );

    let tokenized = app
        .clone()
        .oneshot(request(
            "/tokenize",
            r#"{"model":"first","prompt":"ab","return_token_strs":true}"#,
        ))
        .await
        .unwrap();
    assert_eq!(tokenized.status(), StatusCode::OK);
    let body = axum::body::to_bytes(tokenized.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["count"], 2);
    assert_eq!(body["tokens"], serde_json::json!([97, 98]));
    assert_eq!(body["token_strs"], serde_json::json!(["token", "token"]));

    let detokenized = app
        .clone()
        .oneshot(request("/detokenize", r#"{"tokens":[97,98]}"#))
        .await
        .unwrap();
    assert_eq!(detokenized.status(), StatusCode::OK);

    *models.lock().unwrap() = vec!["second".to_owned()];
    let listed = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(listed.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&body).unwrap()["data"][0]["id"],
        "second"
    );

    let missing = app
        .oneshot(
            Request::builder()
                .uri("/v1/models/first")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(missing.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn openai_http_rejects_unsupported_or_unbounded_requests() {
    let app = router(
        Arc::new(RecordingGeneration::default()),
        Arc::new(|| vec!["served-model".into()]),
        std::time::Duration::from_secs(1),
    );

    for (uri, body) in [
        (
            "/v1/completions",
            r#"{"model":"served-model","prompt":"x","n":17}"#,
        ),
        (
            "/v1/completions",
            r#"{"model":"served-model","prompt":["a","b"],"stream":true}"#,
        ),
        (
            "/v1/completions",
            r#"{"model":"served-model","prompt":"x","kv_transfer_params":{}}"#,
        ),
        (
            "/v1/chat/completions",
            r#"{"model":"served-model","messages":[{"role":"user","content":"x"}],"response_format":{"type":"json_schema","json_schema":{"name":"answer","schema":"not-an-object"}}}"#,
        ),
        (
            "/v1/chat/completions",
            r#"{"model":"served-model","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://169.254.169.254/latest/meta-data"}}]}]}"#,
        ),
    ] {
        let response = app.clone().oneshot(request(uri, body)).await.unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(body["error"]["type"], "invalid_request_error");
        assert_eq!(body["error"]["code"], "invalid_request");
    }
}

#[tokio::test]
async fn completion_stream_and_collected_response_share_terminal_usage_contract() {
    let app = router(
        Arc::new(SuccessfulGeneration),
        Arc::new(|| vec!["served-model".into()]),
        std::time::Duration::from_secs(1),
    );

    let collected = app
        .clone()
        .oneshot(request(
            "/v1/completions",
            r#"{"model":"served-model","prompt":"hello","stream":false}"#,
        ))
        .await
        .unwrap();
    assert_eq!(collected.status(), StatusCode::OK);
    let body = axum::body::to_bytes(collected.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["choices"][0]["text"], "hi");
    assert_eq!(body["choices"][0]["finish_reason"], "length");
    assert_eq!(body["usage"]["prompt_tokens"], 1);
    assert_eq!(body["usage"]["completion_tokens"], 2);

    let streamed = app
        .oneshot(request(
            "/v1/completions",
            r#"{"model":"served-model","prompt":"hello","stream":true,"stream_options":{"include_usage":true}}"#,
        ))
        .await
        .unwrap();
    assert_eq!(streamed.status(), StatusCode::OK);
    let body = axum::body::to_bytes(streamed.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = String::from_utf8(body.to_vec()).unwrap();
    let events = body
        .lines()
        .filter_map(|line| line.strip_prefix("data: "))
        .collect::<Vec<_>>();
    assert_eq!(events.iter().filter(|event| **event == "[DONE]").count(), 1);
    let chunks = events
        .into_iter()
        .filter(|event| *event != "[DONE]")
        .map(|event| serde_json::from_str::<serde_json::Value>(event).unwrap())
        .collect::<Vec<_>>();
    let text = chunks
        .iter()
        .filter_map(|chunk| chunk["choices"].get(0)?.get("text")?.as_str())
        .collect::<String>();
    assert_eq!(text, "hi");
    assert!(chunks.iter().any(|chunk| {
        chunk["choices"]
            .get(0)
            .is_some_and(|choice| choice["finish_reason"] == "length")
    }));
    assert!(
        chunks
            .iter()
            .any(|chunk| chunk["usage"]["prompt_tokens"] == 1)
    );
    assert!(
        chunks
            .iter()
            .any(|chunk| chunk["usage"]["completion_tokens"] == 2)
    );
}

#[tokio::test]
async fn chat_collected_response_preserves_content_finish_reason_and_usage() {
    let app = router(
        Arc::new(SuccessfulGeneration),
        Arc::new(|| vec!["served-model".into()]),
        std::time::Duration::from_secs(1),
    );
    let response = app
        .oneshot(request(
            "/v1/chat/completions",
            r#"{"model":"served-model","messages":[{"role":"user","content":"hello"}],"stream":false,"include_reasoning":true}"#,
        ))
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::OK);
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["choices"][0]["message"]["content"], "hi");
    assert_eq!(body["choices"][0]["finish_reason"], "length");
    assert_eq!(body["usage"]["prompt_tokens"], 1);
    assert_eq!(body["usage"]["completion_tokens"], 2);
    assert!(body["choices"][0]["message"]["reasoning"].is_null());
}
