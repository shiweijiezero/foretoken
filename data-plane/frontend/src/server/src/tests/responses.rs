// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::Arc;

use axum::http::StatusCode;
use futures::stream;

use crate::{
    CompletionResponseOptions, idle_timed, stream_response, text_collected, text_collected_many,
    text_stream, text_stream_with_options,
};

use super::support::{generated_text, generated_text_with_logprobs};

#[tokio::test]
async fn stream_and_nonstream_share_vllm_incremental_decoding() {
    use foretoken_text::TextOutputStreamExt as _;
    use foretoken_text::output::decoded_text_event_stream;
    use foretoken_tokenizer::{Result as TokenizerResult, Tokenizer};
    use futures::StreamExt as _;
    use vllm_llm::{FinishReason, GenerateOutput};

    struct ByteTokenizer;
    impl Tokenizer for ByteTokenizer {
        fn encode(&self, text: &str, _: bool) -> TokenizerResult<Vec<u32>> {
            Ok(text.bytes().map(u32::from).collect())
        }
        fn encode_ordinary(&self, text: &str) -> TokenizerResult<Vec<u32>> {
            self.encode(text, false)
        }
        fn decode(&self, ids: &[u32], _: bool) -> TokenizerResult<String> {
            Ok(ids.iter().filter_map(|&id| char::from_u32(id)).collect())
        }
        fn token_to_id(&self, _: &str) -> Option<u32> {
            None
        }
        fn id_to_token(&self, id: u32) -> Option<String> {
            char::from_u32(id).map(|c| c.to_string())
        }
    }
    let raw = || {
        futures::stream::iter([Ok(GenerateOutput {
            request_id: "request".into(),
            prompt_info: Some(vllm_llm::GeneratePromptInfo {
                prompt_token_ids: vec![1].into(),
                prompt_logprobs: None,
            }),
            token_ids: vec![b'h' as u32, b'i' as u32],
            logprobs: None,
            finish_reason: Some(FinishReason::Length),
            cached_token_count: 0,
            kv_transfer_params: None,
            ec_transfer_params: None,
        })])
    };
    let tokenizer = Arc::new(ByteTokenizer);
    let collected = decoded_text_event_stream(
        "request".into(),
        tokenizer.clone(),
        raw(),
        Default::default(),
        true,
    )
    .collect_output()
    .await
    .unwrap();
    let deltas =
        decoded_text_event_stream("request".into(), tokenizer, raw(), Default::default(), true)
            .filter_map(|event| async move {
                match event.unwrap() {
                    foretoken_text::DecodedTextEvent::TextDelta { delta, .. } => Some(delta),
                    _ => None,
                }
            })
            .collect::<Vec<_>>()
            .await;
    assert_eq!(collected.text, deltas.concat());
    assert_eq!(collected.text, "hi");
}

#[tokio::test]
async fn completion_responses_share_typed_metadata_and_separate_the_terminal_chunk() {
    use vllm_llm::FinishReason;

    let response = text_stream(
        generated_text("cmpl-contract", "served-model", FinishReason::Length),
        std::time::Duration::from_secs(1),
    );
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = std::str::from_utf8(&body).unwrap();
    let chunks = body
        .lines()
        .filter_map(|line| line.strip_prefix("data: "))
        .filter(|data| *data != "[DONE]")
        .map(|data| serde_json::from_str::<serde_json::Value>(data).unwrap())
        .collect::<Vec<_>>();

    assert_eq!(chunks.len(), 2);
    for chunk in &chunks {
        assert_eq!(chunk["id"], "cmpl-contract");
        assert_eq!(chunk["model"], "served-model");
        assert_eq!(chunk["object"], "text_completion");
        assert!(chunk["created"].as_u64().unwrap() > 0);
    }
    assert_eq!(chunks[0]["choices"][0]["text"], "hi");
    assert!(chunks[0]["choices"][0].get("finish_reason").is_none());
    assert_eq!(chunks[1]["choices"][0]["text"], "");
    assert_eq!(chunks[1]["choices"][0]["finish_reason"], "length");

    let response = text_collected(
        generated_text("cmpl-collected", "served-model", FinishReason::Length),
        std::time::Duration::from_secs(1),
    )
    .await;
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["id"], "cmpl-collected");
    assert_eq!(body["model"], "served-model");
    assert!(body["created"].as_u64().unwrap() > 0);
}

#[tokio::test]
async fn completion_raw_ids_require_explicit_opt_in() {
    let response = text_collected_many(
        vec![generated_text(
            "cmpl-ids",
            "served-model",
            vllm_llm::FinishReason::Length,
        )],
        std::time::Duration::from_secs(1),
        CompletionResponseOptions {
            n: 1,
            candidates_per_prompt: 1,
            echo: true,
            expose_logprobs: false,
            return_token_ids: true,
            return_prompt_token_ids: true,
        },
    )
    .await;
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        body["choices"][0]["token_ids"],
        serde_json::json!([104, 105])
    );
    assert_eq!(body["prompt_token_ids"], serde_json::json!([[1]]));
    assert!(body["choices"][0]["text"].as_str().unwrap().ends_with("hi"));
    assert!(body["choices"][0].get("logprobs").is_none());
}

#[tokio::test]
async fn completion_logprobs_usage_and_stop_reason_follow_openai_schema() {
    use vllm_engine_core_client::protocol::output::StopReason;
    use vllm_llm::FinishReason;

    let response = text_stream_with_options(
        generated_text_with_logprobs(
            "cmpl-logprobs-stream",
            "served-model",
            FinishReason::Stop(Some(StopReason::Text("halt".into()))),
        ),
        std::time::Duration::from_secs(1),
        true,
    );
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let chunks = std::str::from_utf8(&body)
        .unwrap()
        .lines()
        .filter_map(|line| line.strip_prefix("data: "))
        .filter(|data| *data != "[DONE]")
        .map(|data| serde_json::from_str::<serde_json::Value>(data).unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        chunks[0]["choices"][0]["logprobs"]["tokens"],
        serde_json::json!(["h", "i"])
    );
    assert_eq!(
        chunks[0]["choices"][0]["logprobs"]["token_logprobs"],
        serde_json::json!([-0.1, -0.2])
    );
    assert!(chunks[0]["choices"][0].get("token_ids").is_none());
    assert_eq!(chunks[1]["choices"][0]["stop_reason"], "halt");
    assert_eq!(chunks[2]["choices"], serde_json::json!([]));
    assert_eq!(
        chunks[2]["usage"]["prompt_tokens_details"]["cached_tokens"],
        1
    );

    let response = text_collected(
        generated_text_with_logprobs("cmpl-logprobs", "served-model", FinishReason::Length),
        std::time::Duration::from_secs(1),
    )
    .await;
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(
        body["choices"][0]["logprobs"]["tokens"],
        serde_json::json!(["h", "i"])
    );
    assert_eq!(body["usage"]["prompt_tokens_details"]["cached_tokens"], 1);
    assert!(body["choices"][0].get("token_ids").is_none());
    assert!(body.get("prompt_token_ids").is_none());
}

#[tokio::test]
async fn error_finish_reason_is_an_openai_error_not_a_successful_finish() {
    use vllm_llm::FinishReason;

    let response = text_collected(
        generated_text("cmpl-error", "served-model", FinishReason::Error),
        std::time::Duration::from_secs(1),
    )
    .await;
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(body["error"]["code"], "request_failed");

    let response = text_stream(
        generated_text("cmpl-error", "served-model", FinishReason::Error),
        std::time::Duration::from_secs(1),
    );
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = std::str::from_utf8(&body).unwrap();
    assert!(body.contains(r#""error":{"message":"generation backend request failed"#));
    assert!(!body.contains(r#""finish_reason":"error"#));
    assert_eq!(body.matches("[DONE]").count(), 1);
}

#[tokio::test]
async fn stream_idle_timeout_terminates_a_silent_output_stream() {
    use futures::StreamExt as _;

    let silent =
        futures::stream::pending::<foretoken_text::Result<foretoken_text::DecodedTextEvent>>();
    let mut timed = Box::pin(idle_timed(silent, std::time::Duration::from_millis(1)));

    assert!(timed.next().await.unwrap().is_err());
    assert!(timed.next().await.is_none());
}

#[tokio::test]
async fn backend_stream_error_emits_one_done_after_the_error() {
    let response = stream_response(stream::iter([Err::<foretoken_text::DecodedTextEvent, _>(
        foretoken_text::Error::StreamClosedBeforeTerminalOutput {
            request_id: "request".into(),
        },
    )]));
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body = std::str::from_utf8(&body).unwrap();
    assert!(body.contains("generation backend request failed"));
    assert_eq!(body.matches("[DONE]").count(), 1);
}
