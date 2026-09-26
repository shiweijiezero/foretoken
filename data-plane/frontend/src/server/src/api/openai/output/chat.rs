// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Encodes chat content, reasoning, tool calls and log probabilities as OpenAI JSON or SSE.

use std::convert::Infallible;
use std::time::Duration;

use axum::Json;
use axum::response::sse::Event;
use axum::response::{IntoResponse, Response};
use foretoken_chat::{AssistantBlockKind, AssistantMessageExt as _, ChatEvent, FinishReason};
use foretoken_text::DecodedLogprobs;
use futures::StreamExt;
use serde::Serialize;

use super::super::openai_error;
use super::{
    OpenAiStopReason, ResponseMetadata, Usage, openai_stop_reason, selected_logprob,
    stream_backend_error,
};
use crate::api::stream::{chat_events, sse_response};
use crate::runtime::{GeneratedChat, GenerationError};

#[derive(Serialize)]
struct ChatLogprobs {
    content: Vec<ChatLogprobContent>,
}

#[derive(Serialize)]
struct ChatLogprobContent {
    token: String,
    logprob: f32,
    bytes: Option<Vec<u8>>,
    top_logprobs: Vec<ChatLogprob>,
}

#[derive(Serialize)]
struct ChatLogprob {
    token: String,
    logprob: f32,
    bytes: Option<Vec<u8>>,
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
struct ToolCallDelta {
    index: usize,
    #[serde(skip_serializing_if = "Option::is_none")]
    id: Option<String>,
    #[serde(rename = "type", skip_serializing_if = "Option::is_none")]
    kind: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    function: Option<FunctionCallDelta>,
}

#[derive(Serialize)]
struct FunctionCallDelta {
    #[serde(skip_serializing_if = "Option::is_none")]
    name: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    arguments: Option<String>,
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
struct ToolCall {
    id: String,
    #[serde(rename = "type")]
    kind: &'static str,
    function: FunctionCall,
}

#[derive(Serialize)]
struct FunctionCall {
    name: String,
    arguments: String,
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
    finish_reason: String,
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
}

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

/// Converts one routed chat stream into the OpenAI SSE response consumed by HTTP clients.
///
/// The chat handler returns this response immediately; its event stream owns backend output until
/// terminal completion, client disconnect, or the configured idle timeout.
pub(crate) fn chat_stream_with_options(
    generated: GeneratedChat,
    idle: Duration,
    include_usage: bool,
) -> Response {
    let metadata = ResponseMetadata::from_generated(&generated.generated);
    let (include_reasoning, stream) = match chat_events(generated, idle) {
        Ok(stream) => stream,
        Err(_) => return openai_error(GenerationError::RequestFailed),
    };
    let events = async_stream::stream! {
        let mut stream = Box::pin(stream);
        let mut saw_tool_calls = false;
        while let Some(event) = stream.next().await {
            let mut finished = false;
            let mut terminal_usage = None;
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
                Ok(ChatEvent::ToolCallStart { index, id, name }) => {
                    saw_tool_calls = true;
                    Some(ChatStreamResponse {
                        metadata: metadata.clone(),
                        object: "chat.completion.chunk",
                        choices: vec![ChatStreamChoice {
                            index: 0,
                            delta: ChatDelta {
                                tool_calls: Some(vec![ToolCallDelta {
                                    index,
                                    id: Some(id),
                                    kind: Some("function"),
                                    function: Some(FunctionCallDelta { name: Some(name), arguments: None }),
                                }]),
                                ..Default::default()
                            },
                            logprobs: None,
                            finish_reason: None,
                            stop_reason: None,
                        }],
                        usage: None,
                    })
                }
                Ok(ChatEvent::ToolCallArgumentsDelta { index, delta }) => Some(ChatStreamResponse {
                    metadata: metadata.clone(),
                    object: "chat.completion.chunk",
                    choices: vec![ChatStreamChoice {
                        index: 0,
                        delta: ChatDelta {
                            tool_calls: Some(vec![ToolCallDelta {
                                index,
                                id: None,
                                kind: None,
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
                Ok(ChatEvent::LogprobsDelta { logprobs, token_ids }) => chat_logprobs(&token_ids, logprobs).map(|logprobs| ChatStreamResponse {
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
                }),
                Ok(ChatEvent::Done {
                    usage,
                    finish_reason,
                    ..
                }) => {
                    let Ok(openai_finish_reason) = chat_finish_reason(&finish_reason, saw_tool_calls) else {
                        yield Ok::<_, Infallible>(Event::default().json_data(stream_backend_error()).unwrap());
                        break;
                    };
                    if include_usage {
                        terminal_usage = Some(Usage::from_counts(
                            usage.prompt_token_count,
                            usage.output_token_count,
                            usage.cached_token_count,
                        ));
                    }
                    finished = true;
                    Some(ChatStreamResponse {
                        metadata: metadata.clone(),
                        object: "chat.completion.chunk",
                        choices: vec![ChatStreamChoice {
                            index: 0,
                            delta: ChatDelta::default(),
                            logprobs: None,
                            finish_reason: Some(openai_finish_reason),
                            stop_reason: openai_stop_reason(&finish_reason),
                        }],
                        usage: None,
                    })
                }
                Ok(ChatEvent::BlockStart { .. }
                    | ChatEvent::BlockDelta { kind: AssistantBlockKind::ToolCall, .. }
                    | ChatEvent::BlockEnd { .. }
                    | ChatEvent::ToolCallEnd { .. }) => None,
                Err(_) => {
                    yield Ok(Event::default().json_data(stream_backend_error()).unwrap());
                    break;
                }
            };
            if let Some(data) = data {
                yield Ok::<_, Infallible>(Event::default().json_data(data).unwrap());
            }
            if finished {
                if let Some(usage) = terminal_usage {
                    yield Ok(Event::default().json_data(ChatStreamResponse {
                        metadata: metadata.clone(),
                        object: "chat.completion.chunk",
                        choices: Vec::new(),
                        usage: Some(usage),
                    }).unwrap());
                }
                break;
            }
        }
        yield Ok(Event::default().data("[DONE]"));
    };
    sse_response(events)
}

/// Collects one routed chat stream into the OpenAI JSON response used for non-streaming requests.
///
/// The chat handler awaits this response through terminal output or idle timeout; any stream error
/// becomes the same typed HTTP error used for failures before collection.
pub(crate) async fn chat_collected(generated: GeneratedChat, idle: Duration) -> Response {
    let metadata = ResponseMetadata::from_generated(&generated.generated);
    let (include_reasoning, stream) = match chat_events(generated, idle) {
        Ok(stream) => stream,
        Err(_) => return openai_error(GenerationError::RequestFailed),
    };
    let mut stream = Box::pin(stream);
    let mut token_ids = Vec::new();
    let mut collected_logprobs: Option<DecodedLogprobs> = None;
    while let Some(event) = stream.next().await {
        match event {
            Ok(ChatEvent::LogprobsDelta {
                logprobs,
                token_ids: delta_token_ids,
            }) => {
                token_ids.extend(delta_token_ids);
                if let Some(delta_logprobs) = logprobs {
                    if let Some(logprobs) = collected_logprobs.as_mut() {
                        logprobs.positions.extend(delta_logprobs.positions);
                    } else {
                        collected_logprobs = Some(delta_logprobs);
                    }
                }
            }
            Ok(ChatEvent::Done {
                message,
                usage,
                finish_reason,
                ..
            }) => {
                let tool_calls = message
                    .tool_calls()
                    .map(|call| ToolCall {
                        id: call.id.clone(),
                        kind: "function",
                        function: FunctionCall {
                            name: call.name.clone(),
                            arguments: call.arguments.clone(),
                        },
                    })
                    .collect::<Vec<_>>();
                let Ok(openai_finish_reason) =
                    chat_finish_reason(&finish_reason, !tool_calls.is_empty())
                else {
                    return openai_error(GenerationError::RequestFailed);
                };
                return Json(ChatResponse {
                    metadata,
                    object: "chat.completion",
                    choices: vec![ChatChoice {
                        index: 0,
                        message: ChatMessage {
                            role: "assistant",
                            // OpenAI represents a tool-only assistant turn with content: null.
                            content: (!message.text().is_empty()).then(|| message.text()),
                            reasoning: include_reasoning.then(|| message.reasoning()).flatten(),
                            tool_calls,
                        },
                        logprobs: chat_logprobs(&token_ids, collected_logprobs),
                        finish_reason: openai_finish_reason.to_owned(),
                        stop_reason: openai_stop_reason(&finish_reason),
                    }],
                    usage: Usage::from_counts(
                        usage.prompt_token_count,
                        usage.output_token_count,
                        usage.cached_token_count,
                    ),
                })
                .into_response();
            }
            Ok(_) => {}
            Err(_) => return openai_error(GenerationError::RequestFailed),
        }
    }
    openai_error(GenerationError::RequestFailed)
}
