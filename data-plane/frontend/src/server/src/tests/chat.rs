// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use tower::ServiceExt;

use crate::{
    Generated, GeneratedChat, RoutedGenerate, RoutedRequest, chat_collected, router, token_stream,
};
use foretoken_chat::{
    ChatMessage, ChatRequest, DefaultChatOutputProcessor, DynChatOutputProcessor,
};
use foretoken_tokenizer::DynTokenizer;

use super::support::{RecordingGeneration, TestTokenizer};

#[tokio::test]
async fn chat_response_uses_vllms_structured_output_processor() {
    use foretoken_model_protocol::ModelServerRole;
    use foretoken_router::{RouteDecision, RouteTargetId};
    use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
    use vllm_llm::{
        FinishReason, GenerateOutput, GeneratePromptInfo, GenerateRequest, Logprobs,
        PositionLogprobs, TokenLogprob,
    };

    let tokenizer: DynTokenizer = Arc::new(TestTokenizer);
    let mut chat = ChatRequest {
        request_id: "chatcmpl-test".into(),
        messages: vec![ChatMessage::User {
            content: "hello".into(),
        }],
        sampling_params: Default::default(),
        chat_options: Default::default(),
        tools: Vec::new(),
        tool_choice: foretoken_chat::ChatToolChoice::None,
        parallel_tool_calls: false,
        decode_options: Default::default(),
        intermediate: true,
        priority: 0,
        documents: None,
        cache_salt: None,
        add_special_tokens: false,
        data_parallel_rank: None,
        session_id: None,
        lora_request: None,
    };
    let tool_call_parser = foretoken_chat::ParserSelection::Auto;
    let reasoning_parser = foretoken_chat::ParserSelection::Auto;
    let output_processor: DynChatOutputProcessor = Box::new(
        DefaultChatOutputProcessor::new(
            &mut chat,
            "test",
            tokenizer.clone(),
            &tool_call_parser,
            &reasoning_parser,
        )
        .unwrap(),
    );
    let request = GenerateRequest {
        request_id: chat.request_id.clone(),
        prompt_token_ids: vec![1],
        sampling_params: EngineCoreSamplingParams::default(),
        mm_features: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        session_id: None,
        reasoning_parser_kwargs: None,
        lora_request: None,
    };
    let generated = GeneratedChat {
        generated: Generated {
            routed: RoutedGenerate {
                routed_request: RoutedRequest {
                    decision: RouteDecision {
                        route_target_id: RouteTargetId::new("group-a"),
                        role: ModelServerRole::Aggregate,
                        model: "test".into(),
                        revision: "r1".into(),
                        data_parallel_rank: 0,
                    },
                    request,
                },
                stream: token_stream(vec![
                    Ok(GenerateOutput {
                        request_id: chat.request_id.clone(),
                        prompt_info: Some(GeneratePromptInfo {
                            prompt_token_ids: vec![1].into(),
                            prompt_logprobs: None,
                        }),
                        token_ids: vec![b'h' as u32],
                        logprobs: Some(Logprobs {
                            positions: vec![PositionLogprobs {
                                entries: vec![TokenLogprob {
                                    token_id: b'h' as u32,
                                    logprob: -0.1,
                                    rank: 1,
                                }],
                            }],
                        }),
                        finish_reason: None,
                        cached_token_count: 0,
                        kv_transfer_params: None,
                        ec_transfer_params: None,
                    }),
                    Ok(GenerateOutput {
                        request_id: chat.request_id,
                        prompt_info: None,
                        token_ids: vec![b'i' as u32],
                        logprobs: Some(Logprobs {
                            positions: vec![PositionLogprobs {
                                entries: vec![TokenLogprob {
                                    token_id: b'i' as u32,
                                    logprob: -0.2,
                                    rank: 1,
                                }],
                            }],
                        }),
                        finish_reason: Some(FinishReason::stop_eos()),
                        cached_token_count: 0,
                        kv_transfer_params: None,
                        ec_transfer_params: None,
                    }),
                ]),
            },
            tokenizer,
            decode_options: chat.decode_options,
        },
        output_processor,
        include_reasoning: true,
    };

    let response = chat_collected(generated, std::time::Duration::from_secs(1)).await;
    let body = axum::body::to_bytes(response.into_body(), usize::MAX)
        .await
        .unwrap();
    let body: serde_json::Value = serde_json::from_slice(&body).unwrap();

    assert_eq!(body["id"], "chatcmpl-test");
    assert_eq!(body["model"], "test");
    assert!(body["created"].as_u64().is_some());
    let message = &body["choices"][0]["message"];
    assert!(message["reasoning"].is_null());
    assert!(message.get("tool_calls").is_none());
    assert_eq!(body["usage"]["prompt_tokens"], 1);
    assert_eq!(body["usage"]["completion_tokens"], 2);
    assert_eq!(body["choices"][0]["logprobs"]["content"][0]["token"], "h");
    assert_eq!(
        body["choices"][0]["logprobs"]["content"][0]["logprob"],
        -0.1
    );
    assert!(body.get("token_ids").is_none());
}

#[tokio::test]
async fn unsupported_chat_features_and_prompt_dto_on_chat_are_client_errors() {
    let app = router(
        Arc::new(RecordingGeneration::default()),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    for body in [
        r#"{"model":"m","prompt":"wrong"}"#,
        r#"{"model":"m","messages":[{"role":"user","content":"x"}],"chat_template":"forbidden"}"#,
        r#"{"model":"m","messages":[{"role":"user","content":"x"}],"unsupported":true}"#,
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/chat/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(body))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert!(response.status().is_client_error());
        let body = axum::body::to_bytes(response.into_body(), usize::MAX)
            .await
            .unwrap();
        let body: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(body["error"]["type"], "invalid_request_error");
        assert_eq!(body["error"]["code"], "invalid_request");
    }
}

#[tokio::test]
async fn chat_maps_tool_history_choice_reasoning_and_structured_output() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let response = app.oneshot(Request::builder().method("POST").uri("/v1/chat/completions").header("content-type", "application/json").body(Body::from(r#"{"model":"m","include_reasoning":false,"parallel_tool_calls":true,"tools":[{"type":"function","function":{"name":"weather","parameters":{"type":"object"}}}],"tool_choice":{"type":"function","function":{"name":"weather"}},"response_format":{"type":"json_object"},"session_id":"session-1","messages":[{"role":"assistant","content":null,"tool_calls":[{"id":"call_1","type":"function","function":{"name":"weather","arguments":"{}"}}]},{"role":"tool","tool_call_id":"call_1","content":"sunny"},{"role":"user","content":"continue"}]}"#)).unwrap()).await.unwrap();
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    let captured = generation.chat_requests.lock().unwrap();
    let (chat, include_reasoning) = &captured[0];
    assert!(!include_reasoning);
    assert_eq!(chat.tools[0].name, "weather");
    assert_eq!(
        chat.tool_choice,
        foretoken_chat::ChatToolChoice::Function {
            name: "weather".into()
        }
    );
    assert!(chat.parallel_tool_calls);
    assert_eq!(chat.session_id.as_deref(), Some("session-1"));
    assert!(matches!(chat.messages[0], ChatMessage::Assistant { .. }));
    assert!(
        matches!(chat.messages[1], ChatMessage::ToolResponse { ref tool_call_id, .. } if tool_call_id == "call_1")
    );
    assert!(chat.sampling_params.structured_outputs.is_some());
}

#[tokio::test]
async fn chat_accepts_images_and_rejects_unsupported_media() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let image = r#"{"model":"m","messages":[{"role":"system","content":"inspect media"},{"role":"user","content":[{"type":"text","text":"describe"},{"type":"image_url","image_url":{"url":"data:image/png;base64,AA==","detail":"low"}}]}]}"#;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .body(Body::from(image))
                .unwrap(),
        )
        .await
        .unwrap();

    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    {
        let captured = generation.chat_requests.lock().unwrap();
        let (chat, _) = &captured[0];
        assert!(matches!(
            chat.messages[1],
            ChatMessage::User { ref content } if content.has_multimodal()
        ));
    }

    for unsupported in [
        r#"{"model":"m","messages":[{"role":"user","content":[{"type":"video_url","video_url":{"url":"data:video/mp4;base64,AA=="}}]}]}"#,
        r#"{"model":"m","messages":[{"role":"user","content":[{"type":"input_audio","input_audio":{"data":"AA==","format":"wav"}}]}]}"#,
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/chat/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(unsupported))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
    assert_eq!(generation.chat_requests.lock().unwrap().len(), 1);
}

#[tokio::test]
async fn chat_reasoning_and_json_schema_are_capability_gated_and_invalid_extensions_reject() {
    let generation = Arc::new(RecordingGeneration::default());
    let app = router(
        generation.clone(),
        Arc::new(Vec::new),
        std::time::Duration::from_secs(1),
    );
    let valid = r#"{"model":"m","reasoning_effort":"high","include_reasoning":true,"response_format":{"type":"json_schema","json_schema":{"name":"answer","schema":{"type":"object"}}},"messages":[{"role":"user","content":"x"}]}"#;
    let response = app
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/v1/chat/completions")
                .header("content-type", "application/json")
                .body(Body::from(valid))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::BAD_GATEWAY);
    {
        let captured = generation.chat_requests.lock().unwrap();
        assert!(captured[0].0.sampling_params.structured_outputs.is_some());
        assert!(captured[0].1);
    }
    for invalid in [
        r#"{"model":"m","messages":[{"role":"user","content":"x"}],"kv_transfer_params":{}}"#,
        r#"{"model":"m","messages":[{"role":"user","content":"x"}],"response_format":{"type":"json_schema","json_schema":{"name":"answer","schema":"not-an-object"}}}"#,
        r#"{"model":"m","messages":[{"role":"user","content":[{"type":"image_url","image_url":{"url":"https://169.254.169.254/latest/meta-data"}}]}]}"#,
    ] {
        let response = app
            .clone()
            .oneshot(
                Request::builder()
                    .method("POST")
                    .uri("/v1/chat/completions")
                    .header("content-type", "application/json")
                    .body(Body::from(invalid))
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::BAD_REQUEST);
    }
}
