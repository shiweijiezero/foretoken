// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Encodes chat content, reasoning, tool calls and log probabilities as OpenAI JSON or SSE.

use std::convert::Infallible;
use std::time::Duration;

use async_openai::types::chat::{
    ChatCompletionMessageToolCall, ChatCompletionMessageToolCallChunk as ToolCallDelta,
    ChatCompletionMessageToolCalls as ToolCall, ChatCompletionTokenLogprob as ChatLogprobContent,
    FunctionCall, FunctionCallStream as FunctionCallDelta, FunctionType,
    TopLogprobs as ChatLogprob,
};
use axum::Json;
use axum::response::sse::Event;
use axum::response::{IntoResponse, Response};
use foretoken_chat::{
    AssistantBlockKind, AssistantMessage, AssistantMessageExt as _, ChatEvent, FinishReason,
};
use foretoken_text::DecodedLogprobs;
use futures::StreamExt;
use serde::Serialize;
use serde_json::{Value, json};

use super::super::openai_error;
use super::super::stored::ChatArchive;
use super::{
    OpenAiStopReason, ResponseMetadata, Usage, openai_stop_reason, selected_logprob,
    stream_backend_error,
};
use crate::api::stream::{chat_events, sse_response};
use crate::runtime::{GeneratedChat, GenerationError};

#[derive(Clone, Default, Serialize)]
struct ChatLogprobs {
    content: Vec<ChatLogprobContent>,
}

#[derive(Default, Serialize)]
struct ChatDelta {
    #[serde(skip_serializing_if = "Option::is_none")]
    role: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    content: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    reasoning: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    tool_calls: Option<Vec<ToolCallDelta>>,
}

#[derive(Serialize)]
struct ChatStreamChoice {
    index: u32,
    delta: ChatDelta,
    #[serde(skip_serializing_if = "Option::is_none")]
    logprobs: Option<ChatLogprobs>,
    #[serde(skip_serializing_if = "Option::is_none")]
    finish_reason: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    stop_reason: Option<OpenAiStopReason>,
}

#[derive(Serialize)]
struct ChatStreamResponse {
    #[serde(flatten)]
    metadata: ResponseMetadata,
    object: &'static str,
    choices: Vec<ChatStreamChoice>,
    #[serde(skip_serializing_if = "Option::is_none")]
    usage: Option<Usage>,
}

#[derive(Serialize)]
struct ChatMessage {
    role: &'static str,
    content: Option<String>,
    #[serde(skip_serializing_if = "Vec::is_empty")]
    tool_calls: Vec<ToolCall>,
    reasoning: Option<String>,
}

#[derive(Serialize)]
struct ChatChoice {
    index: u32,
    message: ChatMessage,
    #[serde(skip_serializing_if = "Option::is_none")]
    logprobs: Option<ChatLogprobs>,
    finish_reason: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    stop_reason: Option<OpenAiStopReason>,
}

#[derive(Serialize)]
struct ChatResponse {
    #[serde(flatten)]
    metadata: ResponseMetadata,
    object: &'static str,
    choices: Vec<ChatChoice>,
    usage: Usage,
    #[serde(rename = "metadata")]
    custom_metadata: Value,
}

/// Streams parser events directly to OpenAI SSE, committing requested archives before terminal output.
/// Dropping the HTTP stream retains the existing backend cancellation ownership.
pub(crate) fn chat_stream_with_options(
    generated: GeneratedChat,
    idle: Duration,
    include_usage: bool,
    custom_metadata: Value,
    archive: Option<ChatArchive>,
) -> Response {
    let metadata = ResponseMetadata::from_generated(&generated.generated);
    let custom_metadata = archive
        .as_ref()
        .map_or(custom_metadata, |archive| archive.metadata.clone());
    let (include_reasoning, stream) = match chat_events(generated, idle) {
        Ok(stream) => stream,
        Err(_) => return openai_error(GenerationError::RequestFailed),
    };
    let events = async_stream::stream! {
        let mut stream = Box::pin(stream);
        let mut collected_logprobs: Option<ChatLogprobs> = None;
        while let Some(event) = stream.next().await {
            let data = match event {
                Ok(ChatEvent::Start { .. }) => Some(ChatStreamResponse {
                    metadata: metadata.clone(),
                    object: "chat.completion.chunk",
                    choices: vec![ChatStreamChoice {
                        index: 0,
                        delta: ChatDelta { role: Some("assistant"), ..Default::default() },
                        logprobs: None,
                        finish_reason: None,
                        stop_reason: None,
                    }],
                    usage: None,
                }),
                Ok(ChatEvent::BlockDelta { kind: AssistantBlockKind::Text, delta, .. }) => Some(ChatStreamResponse {
                    metadata: metadata.clone(),
                    object: "chat.completion.chunk",
                    choices: vec![ChatStreamChoice {
                        index: 0,
                        delta: ChatDelta { content: Some(delta), ..Default::default() },
                        logprobs: None,
                        finish_reason: None,
                        stop_reason: None,
                    }],
                    usage: None,
                }),
                Ok(ChatEvent::BlockDelta { kind: AssistantBlockKind::Reasoning, delta, .. }) if include_reasoning => Some(ChatStreamResponse {
                    metadata: metadata.clone(),
                    object: "chat.completion.chunk",
                    choices: vec![ChatStreamChoice {
                        index: 0,
                        delta: ChatDelta { reasoning: Some(delta), ..Default::default() },
                        logprobs: None,
                        finish_reason: None,
                        stop_reason: None,
                    }],
                    usage: None,
                }),
                Ok(ChatEvent::BlockDelta { kind: AssistantBlockKind::Reasoning, .. }) => None,
                Ok(ChatEvent::ToolCallStart { index, id, name }) => Some(ChatStreamResponse {
                    metadata: metadata.clone(),
                    object: "chat.completion.chunk",
                    choices: vec![ChatStreamChoice {
                        index: 0,
                        delta: ChatDelta {
                            tool_calls: Some(vec![ToolCallDelta {
                                index: index as u32,
                                id: Some(id),
                                r#type: Some(FunctionType::Function),
                                function: Some(FunctionCallDelta { name: Some(name), arguments: None }),
                            }]),
                            ..Default::default()
                        },
                        logprobs: None,
                        finish_reason: None,
                        stop_reason: None,
                    }],
                    usage: None,
                }),
                Ok(ChatEvent::ToolCallArgumentsDelta { index, delta }) => Some(ChatStreamResponse {
                    metadata: metadata.clone(),
                    object: "chat.completion.chunk",
                    choices: vec![ChatStreamChoice {
                        index: 0,
                        delta: ChatDelta {
                            tool_calls: Some(vec![ToolCallDelta {
                                index: index as u32,
                                id: None,
                                r#type: None,
                                function: Some(FunctionCallDelta { name: None, arguments: Some(delta) }),
                            }]),
                            ..Default::default()
                        },
                        logprobs: None,
                        finish_reason: None,
                        stop_reason: None,
                    }],
                    usage: None,
                }),
                Ok(ChatEvent::LogprobsDelta { logprobs, token_ids }) => {
                    let logprobs = chat_logprobs(&token_ids, logprobs);
                    if archive.is_some() && let Some(delta) = &logprobs {
                        collected_logprobs.get_or_insert_with(ChatLogprobs::default)
                            .content.extend(delta.content.clone());
                    }
                    logprobs.map(|logprobs| ChatStreamResponse {
                        metadata: metadata.clone(),
                        object: "chat.completion.chunk",
                        choices: vec![ChatStreamChoice {
                            index: 0,
                            delta: ChatDelta::default(),
                            logprobs: Some(logprobs),
                            finish_reason: None,
                            stop_reason: None,
                        }],
                        usage: None,
                    })
                }
                Ok(ChatEvent::Done { message, usage, finish_reason, .. }) => {
                    let response = match build_chat_response(
                        metadata.clone(), &message, usage, &finish_reason,
                        include_reasoning, collected_logprobs, custom_metadata,
                    ) {
                        Ok(response) => response,
                        Err(_) => {
                            drop(stream);
                            yield Ok::<_, Infallible>(Event::default().json_data(stream_backend_error()).unwrap());
                            return;
                        }
                    };
                    // Backend completion precedes storage and any terminal yield to a slow reader.
                    drop(stream);
                    if let Some(archive) = &archive {
                        let body = serde_json::to_value(&response).expect("chat response serialization");
                        if let Err(error) = archive.save(&body).await {
                            yield Ok::<_, Infallible>(Event::default().json_data(json!({"error": {
                                "message": error.to_string(), "type": "server_error", "code": "storage_error",
                            }})).unwrap());
                            return;
                        }
                    }
                    let choice = response.choices.into_iter().next().expect("one chat choice");
                    yield Ok::<_, Infallible>(Event::default().json_data(ChatStreamResponse {
                        metadata: metadata.clone(),
                        object: "chat.completion.chunk",
                        choices: vec![ChatStreamChoice {
                            index: 0,
                            delta: ChatDelta::default(),
                            logprobs: None,
                            finish_reason: Some(choice.finish_reason),
                            stop_reason: choice.stop_reason,
                        }],
                        usage: None,
                    }).unwrap());
                    if include_usage {
                        yield Ok(Event::default().json_data(ChatStreamResponse {
                            metadata: metadata.clone(),
                            object: "chat.completion.chunk",
                            choices: Vec::new(),
                            usage: Some(response.usage),
                        }).unwrap());
                    }
                    yield Ok(Event::default().data("[DONE]"));
                    return;
                }
                Ok(ChatEvent::BlockStart { .. }
                    | ChatEvent::BlockDelta { kind: AssistantBlockKind::ToolCall, .. }
                    | ChatEvent::BlockEnd { .. }
                    | ChatEvent::ToolCallEnd { .. }) => None,
                Err(_) => {
                    drop(stream);
                    yield Ok::<_, Infallible>(Event::default().json_data(stream_backend_error()).unwrap());
                    return;
                }
            };
            if let Some(data) = data {
                yield Ok::<_, Infallible>(Event::default().json_data(data).unwrap());
            }
        }
        drop(stream);
        yield Ok(Event::default().json_data(stream_backend_error()).unwrap());
    };
    sse_response(events)
}

/// Collects parser output into the same canonical completion used by stream archives.
/// Requested storage commits before JSON success; failures retain their typed HTTP status.
pub(crate) async fn chat_collected(
    generated: GeneratedChat,
    idle: Duration,
    custom_metadata: Value,
    archive: Option<ChatArchive>,
) -> Response {
    let metadata = ResponseMetadata::from_generated(&generated.generated);
    let custom_metadata = archive
        .as_ref()
        .map_or(custom_metadata, |archive| archive.metadata.clone());
    let (include_reasoning, stream) = match chat_events(generated, idle) {
        Ok(stream) => stream,
        Err(_) => return openai_error(GenerationError::RequestFailed),
    };
    let mut stream = Box::pin(stream);
    let mut collected_logprobs: Option<ChatLogprobs> = None;
    while let Some(event) = stream.next().await {
        match event {
            Ok(ChatEvent::LogprobsDelta {
                logprobs,
                token_ids,
            }) => {
                if let Some(delta) = chat_logprobs(&token_ids, logprobs) {
                    collected_logprobs
                        .get_or_insert_with(ChatLogprobs::default)
                        .content
                        .extend(delta.content);
                }
            }
            Ok(ChatEvent::Done {
                message,
                usage,
                finish_reason,
                ..
            }) => {
                let response = match build_chat_response(
                    metadata,
                    &message,
                    usage,
                    &finish_reason,
                    include_reasoning,
                    collected_logprobs,
                    custom_metadata,
                ) {
                    Ok(response) => response,
                    Err(error) => return openai_error(error),
                };
                drop(stream);
                if let Some(archive) = &archive {
                    let body =
                        serde_json::to_value(&response).expect("chat response serialization");
                    if let Err(error) = archive.save(&body).await {
                        return crate::api::store_error(error);
                    }
                }
                return Json(response).into_response();
            }
            Ok(_) => {}
            Err(_) => return openai_error(GenerationError::RequestFailed),
        }
    }
    openai_error(GenerationError::RequestFailed)
}

/// Assembles the canonical public completion from parser-owned final output for both response modes.
fn build_chat_response(
    metadata: ResponseMetadata,
    message: &AssistantMessage,
    usage: vllm_llm::TokenUsage,
    finish_reason: &FinishReason,
    include_reasoning: bool,
    logprobs: Option<ChatLogprobs>,
    custom_metadata: Value,
) -> Result<ChatResponse, GenerationError> {
    let tool_calls = message
        .tool_calls()
        .map(|call| {
            ToolCall::Function(ChatCompletionMessageToolCall {
                id: call.id.clone(),
                function: FunctionCall {
                    name: call.name.clone(),
                    arguments: call.arguments.clone(),
                },
            })
        })
        .collect::<Vec<_>>();
    let finish = chat_finish_reason(finish_reason, !tool_calls.is_empty())
        .map_err(|_| GenerationError::RequestFailed)?;
    let content = message.text();
    Ok(ChatResponse {
        metadata,
        object: "chat.completion",
        choices: vec![ChatChoice {
            index: 0,
            message: ChatMessage {
                role: "assistant",
                // OpenAI represents a tool-only assistant turn with content: null.
                content: (!content.is_empty()).then_some(content),
                reasoning: include_reasoning.then(|| message.reasoning()).flatten(),
                tool_calls,
            },
            logprobs,
            finish_reason: finish,
            stop_reason: openai_stop_reason(finish_reason),
        }],
        usage: Usage::from_counts(
            usage.prompt_token_count,
            usage.output_token_count,
            usage.cached_token_count,
        ),
        custom_metadata,
    })
}

/// Separates the sampled-token score from the engine's ordered top-k candidates.
fn chat_logprobs(token_ids: &[u32], logprobs: Option<DecodedLogprobs>) -> Option<ChatLogprobs> {
    let logprobs = logprobs?;
    let content = token_ids
        .iter()
        .copied()
        .zip(logprobs.positions)
        .map(|(token_id, position)| {
            let selected = selected_logprob(&position, token_id)
                .map(|entry| (entry.token.clone(), entry.logprob.max(-9999.0)));
            let token = selected.as_ref().map_or_else(
                || format!("token_id:{token_id}"),
                |(token, _)| token.clone(),
            );
            let top_logprobs = position
                .entries
                .into_iter()
                .skip(1)
                .map(|entry| ChatLogprob {
                    bytes: Some(entry.token.as_bytes().to_vec()),
                    token: entry.token,
                    logprob: entry.logprob.max(-9999.0),
                })
                .collect();
            ChatLogprobContent {
                bytes: Some(token.as_bytes().to_vec()),
                token,
                logprob: selected.map_or(-9999.0, |(_, logprob)| logprob),
                top_logprobs,
            }
        })
        .collect();
    Some(ChatLogprobs { content })
}

fn chat_finish_reason(
    finish_reason: &FinishReason,
    has_tool_calls: bool,
) -> Result<&'static str, ()> {
    match finish_reason {
        FinishReason::Stop(_) if has_tool_calls => Ok("tool_calls"),
        FinishReason::Stop(_) => Ok("stop"),
        FinishReason::Length => Ok("length"),
        FinishReason::Abort => Ok("abort"),
        FinishReason::Repetition(_) => Ok("repetition"),
        FinishReason::Error => Err(()),
    }
}
