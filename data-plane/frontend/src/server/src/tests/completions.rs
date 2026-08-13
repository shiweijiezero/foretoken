// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::{Arc, Mutex};

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use crate::router;
use foretoken_text::Prompt;

use super::support::RecordingGeneration;

#[tokio::test]
async fn text_and_chat_routes_use_their_respective_generation_paths() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(|| vec!["first".into()]),
        std::time::Duration::from_secs(1),
    );
    for (uri, body) in [
        (
            "/v1/completions",
            r#"{"model":"m","prompt":"text","request_id":"reused-by-client"}"#,
        ),
        (
            "/v1/chat/completions",
            r#"{"model":"m","messages":[{"role":"user","content":"chat"}],"request_id":"reused-by-client"}"#,
        ),
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri(uri)
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }
    let calls = generation.calls.lock().unwrap();
    assert_eq!(calls[0].0, "text");
    assert_eq!(calls[1], ("chat".into(), "chat".into()));
    drop(calls);
    let request_ids = generation.request_ids.lock().unwrap();
    assert!(request_ids.iter().all(|id| id != "reused-by-client"));
    assert!(request_ids.iter().any(|id| id.starts_with("cmpl-")));
    assert!(request_ids.iter().any(|id| id.starts_with("chatcmpl-")));
    drop(request_ids);
}

#[tokio::test]
async fn completion_preserves_token_prompts_and_supported_sampling_fields() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let body = r#"{"model":"m","prompt":[1,2,3],"stream":true,"temperature":0.4,"top_p":0.8,"top_k":20,"min_p":0.1,"seed":7,"max_tokens":64,"min_tokens":2,"frequency_penalty":0.2,"presence_penalty":0.3,"repetition_penalty":1.1,"stop_token_ids":[9],"ignore_eos":true,"logit_bias":{"10":-2.0},"allowed_token_ids":[10,11],"bad_words":["bad"],"logprobs":5,"prompt_logprobs":3,"priority":4,"cache_salt":"tenant","session_id":"session-1"}"#;
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/completions")
                .header("content-type", "application/json")
                .body(Body::from(body))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
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
}

#[tokio::test]
async fn completion_accepts_prompt_forms_and_rejects_unbounded_fanout() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/completions")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"model":"m","prompt":["first","second"],"n":2,"best_of":3}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    {
        let captured = generation.text_requests.lock().unwrap();
        // The recording backend rejects the first candidate, but the request has already been
        // lowered as the first member of the bounded multi-prompt/best-of fan-out.
        assert_eq!(captured.len(), 1);
        assert!(matches!(captured[0].0, Prompt::Text(ref text) if text == "first"));
        assert_eq!(captured[0].1.logprobs, Some(0));
    }

    for prompt in [r#"[1,2,3]"#, r#"[[4,5],[6]]"#] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(format!(r#"{{"model":"m","prompt":{prompt}}}"#)))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }
    let response = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/completions")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"model":"m","prompt":"x","n":17}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_REQUEST);
}

#[tokio::test]
async fn completion_rejects_streaming_multi_prompt_best_of_and_transfer_injection() {
    let app = router(
        Arc::new(RecordingGeneration::default()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    for body in [
        r#"{"model":"m","prompt":["a","b"],"stream":true}"#,
        r#"{"model":"m","prompt":"a","stream":true,"best_of":2}"#,
        r#"{"model":"m","prompt":"a","kv_transfer_params":{}}"#,
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}

#[tokio::test]
async fn generated_request_ids_are_unique_and_models_are_dynamic() {
    let generation = Arc::new(RecordingGeneration::default());
    let models = Arc::new(Mutex::new(vec!["first".to_owned()]));
    let source = models.clone();
    let app = router(
        generation.clone(),
        Arc::new(move || source.lock().unwrap().clone()),
        std::time::Duration::from_secs(1),
    );
    for _ in 0..2 {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(r#"{"model":"m","prompt":"p"}"#))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    }
    {
        let calls = generation.calls.lock().unwrap();
        assert_ne!(calls[0].1, calls[1].1);
    }
    *models.lock().unwrap() = vec!["second".to_owned()];
    let response = app
        .oneshot(
            Request::builder()
                .uri("/v1/models")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    assert!(std::str::from_utf8(&body).unwrap().contains("second"));
}

#[tokio::test]
async fn concurrent_reused_client_request_ids_get_distinct_route_target_ids() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let requests = (0..2).map(|_| {
        app.clone().oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/completions")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"model":"m","prompt":"p","request_id":"reused-by-client"}"#,
                ))
                .unwrap(),
        )
    });

    let responses = futures::future::join_all(requests).await;
    assert!(
        responses
            .iter()
            .all(|response| response.as_ref().unwrap().status() == StatusCode::BAD_GATEWAY)
    );
    let request_ids = generation.request_ids.lock().unwrap();
    assert_eq!(request_ids.len(), 2);
    assert!(request_ids.iter().all(|id| id.starts_with("cmpl-")));
    assert!(request_ids.iter().all(|id| id != "reused-by-client"));
    assert_ne!(request_ids[0], request_ids[1]);
}
