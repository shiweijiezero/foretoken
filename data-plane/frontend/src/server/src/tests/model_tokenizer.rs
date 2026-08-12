// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use crate::router;

use super::support::RecordingGeneration;

#[tokio::test]
async fn model_and_tokenizer_endpoints_follow_vllm_shapes() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation,
        Arc::new(|| vec!["m".into()]),
        std::time::Duration::from_secs(1),
    );

    let model = app
        .clone()
        .oneshot(
            Request::builder()
                .uri("/v1/models/m")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(model.status(), StatusCode::OK);

    let tokenized = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/tokenize")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{"model":"m","prompt":"ab","return_token_strs":true}"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(tokenized.status(), StatusCode::OK);
    let body = axum::body::to_bytes(tokenized.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["count"], 2);
    assert_eq!(body["max_model_len"], 4096);
    assert_eq!(body["tokens"], serde_json::json!([97, 98]));

    let detokenized = app
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/detokenize")
                .header("content-type", "application/json")
                .body(Body::from(r#"{"model":"m","tokens":[97,98]}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(detokenized.status(), StatusCode::OK);
    let body = axum::body::to_bytes(detokenized.into_body(), usize::MAX)
        .await
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<serde_json::Value>(&body).unwrap()["prompt"],
        "ab"
    );
}
