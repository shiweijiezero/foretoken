// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Encodes shared chat events into Anthropic messages without an intermediate HTTP protocol.

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::time::Duration;

use axum::Json;
use axum::response::sse::Event;
use axum::response::{IntoResponse, Response};
use foretoken_chat::{AssistantBlockKind, AssistantContentBlock, ChatEvent, FinishReason};
use foretoken_engine_core_client::protocol::output::StopReason as EngineStopReason;
use futures::StreamExt;
use serde_json::{Value, json};

use super::error::AnthropicApiError;
use super::types::{AnthropicMessagesResponse, AnthropicUsage, ResponseContentBlock, StopReason};
use crate::api::stream::{chat_events, sse_response};
use crate::runtime::GeneratedChat;

/// Collects the canonical chat stream into one Anthropic message.
pub(super) async fn collected(generated: GeneratedChat, idle: Duration) -> Response {
    let id = generated
        .generated
        .routed
        .routed_request
        .request
        .request_id
        .clone();
    let model = generated
        .generated
        .routed
        .routed_request
        .decision
        .model
        .clone();
    let (include_reasoning, stream) = match chat_events(generated, idle) {
        Ok(events) => events,
        Err(_) => return AnthropicApiError::stream().into_response(),
    };
    let mut stream = Box::pin(stream);
    while let Some(event) = stream.next().await {
        match event {
            Ok(ChatEvent::Done {
                message,
                usage,
                finish_reason,
                ..
            }) => {
                let truncated = matches!(finish_reason, FinishReason::Length);
                let content = match message
                    .content
                    .into_iter()
                    .filter(|block| {
                        include_reasoning
                            || !matches!(block, AssistantContentBlock::Reasoning { .. })
                    })
                    .filter_map(|block| content_block(block, truncated).transpose())
                    .collect::<Result<Vec<_>, _>>()
                {
                    Ok(content) => content,
                    Err(error) => return error.into_response(),
                };
                let has_tools = content
                    .iter()
                    .any(|block| matches!(block, ResponseContentBlock::ToolUse { .. }));
                let (stop_reason, stop_sequence) = match stop(finish_reason, has_tools) {
                    Ok(stop) => stop,
                    Err(error) => return error.into_response(),
                };
                return Json(AnthropicMessagesResponse {
                    id,
                    response_type: "message",
                    role: "assistant",
                    model,
                    content,
                    stop_reason: Some(stop_reason),
                    stop_sequence,
                    usage: token_usage(
                        usage.prompt_token_count,
                        usage.output_token_count,
                        usage.cached_token_count,
                    ),
                })
                .into_response();
            }
            Err(_) => return AnthropicApiError::stream().into_response(),
            _ => {}
        }
    }
    AnthropicApiError::stream().into_response()
}

/// Emits Anthropic SSE directly from the chat processor; dropping the body cancels generation.
pub(super) fn streaming(generated: GeneratedChat, idle: Duration) -> Response {
    let id = generated
        .generated
        .routed
        .routed_request
        .request
        .request_id
        .clone();
    let model = generated
        .generated
        .routed
        .routed_request
        .decision
        .model
        .clone();
    let (include_reasoning, stream) = match chat_events(generated, idle) {
        Ok(events) => events,
        Err(_) => return AnthropicApiError::stream().into_response(),
    };
    let stream = async_stream::stream! {
        let mut stream = Box::pin(stream);
        let mut blocks = StreamBlocks::default();
        let mut started = false;
        while let Some(event) = stream.next().await {
            let event = match event {
                Ok(event) => event,
                Err(_) => {
                    drop(stream);
                    yield Ok::<_, Infallible>(sse("error", AnthropicApiError::stream().body()));
                    return;
                }
            };
            let encoded = match event {
                ChatEvent::Start { prompt_token_ids, .. } => {
                    started = true;
                    Ok(Some(sse("message_start", json!({"type":"message_start", "message": {
                        "id":id, "type":"message", "role":"assistant", "model":model,
                        "content":[], "stop_reason":null, "stop_sequence":null,
                        "usage":{"input_tokens":prompt_token_ids.len(), "output_tokens":0}
                    }}))))
                }
                ChatEvent::BlockStart { index, kind } => {
                    match kind {
                        AssistantBlockKind::Text => blocks.start((0,index), json!({"type":"text","text":""})),
                        AssistantBlockKind::Reasoning if include_reasoning => blocks.start((0,index), json!({"type":"thinking","thinking":""})),
                        _ => Ok(None),
                    }
                }
                ChatEvent::BlockDelta { index, kind, delta } => {
                    match kind {
                        AssistantBlockKind::Text => blocks.delta((0,index), json!({"type":"text_delta","text":delta})),
                        AssistantBlockKind::Reasoning if include_reasoning => blocks.delta((0,index), json!({"type":"thinking_delta","thinking":delta})),
                        _ => Ok(None),
                    }
                }
                ChatEvent::BlockEnd { index, block } => {
                    match block {
                        AssistantContentBlock::Text { .. } => blocks.end((0,index)),
                        AssistantContentBlock::Reasoning { .. } if include_reasoning => blocks.end((0,index)),
                        _ => Ok(None),
                    }
                }
                ChatEvent::ToolCallStart { index, id, name } => {
                    blocks.has_tools = true;
                    blocks.start((1,index), json!({"type":"tool_use", "id":id, "name":name, "input":{}}))
                }
                ChatEvent::ToolCallArgumentsDelta { index, delta } => {
                    blocks.delta((1,index), json!({"type":"input_json_delta", "partial_json":delta}))
                }
                ChatEvent::ToolCallEnd { index, .. } => blocks.end((1,index)),
                ChatEvent::LogprobsDelta { .. } => Ok(None),
                ChatEvent::Done { message, usage, finish_reason, .. } => {
                    // Partial tool JSON is recoverable at the output limit, not a backend failure.
                    // The terminal event owns this decision; parser recovery may emit intervening text.
                    let truncated = matches!(finish_reason, FinishReason::Length);
                    let valid_tools = message.content.iter().all(|block| match block {
                        AssistantContentBlock::ToolCall(call) => tool_input(&call.arguments, truncated).is_ok(),
                        _ => true,
                    });
                    let terminal = if valid_tools {
                        stop(finish_reason, blocks.has_tools)
                    } else {
                        Err(AnthropicApiError::stream())
                    };
                    match terminal {
                        Ok((reason, sequence)) if started && blocks.open.is_empty() => {
                            drop(stream);
                            yield Ok(sse("message_delta", json!({
                                "type":"message_delta", "delta":{"stop_reason":reason,"stop_sequence":sequence},
                                "usage":token_usage(usage.prompt_token_count, usage.output_token_count, usage.cached_token_count)
                            })));
                            yield Ok(sse("message_stop", json!({"type":"message_stop"})));
                            return;
                        }
                        _ => Err(AnthropicApiError::stream()),
                    }
                }
            };
            match encoded {
                Ok(Some(event)) if started => yield Ok(event),
                Ok(None) => {}
                _ => {
                    drop(stream);
                    yield Ok(sse("error", AnthropicApiError::stream().body()));
                    return;
                }
            }
        }
        drop(stream);
        yield Ok(sse("error", AnthropicApiError::stream().body()));
    };
    sse_response(stream)
}

/// Maps the chat processor's independent text/tool indices into one Anthropic block sequence.
#[derive(Default)]
struct StreamBlocks {
    next: usize,
    open: BTreeMap<(u8, usize), usize>,
    has_tools: bool,
}

impl StreamBlocks {
    fn start(
        &mut self,
        key: (u8, usize),
        block: Value,
    ) -> Result<Option<Event>, AnthropicApiError> {
        if self.open.contains_key(&key) {
            return Err(AnthropicApiError::stream());
        }
        let index = self.next;
        self.next += 1;
        self.open.insert(key, index);
        Ok(Some(sse(
            "content_block_start",
            json!({"type":"content_block_start","index":index,"content_block":block}),
        )))
    }

    fn delta(&self, key: (u8, usize), delta: Value) -> Result<Option<Event>, AnthropicApiError> {
        let index = self.open.get(&key).ok_or_else(AnthropicApiError::stream)?;
        Ok(Some(sse(
            "content_block_delta",
            json!({"type":"content_block_delta","index":index,"delta":delta}),
        )))
    }

    fn end(&mut self, key: (u8, usize)) -> Result<Option<Event>, AnthropicApiError> {
        let index = self
            .open
            .remove(&key)
            .ok_or_else(AnthropicApiError::stream)?;
        Ok(Some(sse(
            "content_block_stop",
            json!({"type":"content_block_stop","index":index}),
        )))
    }
}

/// Converts collected content, omitting incomplete tool calls when the output budget was exhausted.
fn content_block(
    block: AssistantContentBlock,
    truncated: bool,
) -> Result<Option<ResponseContentBlock>, AnthropicApiError> {
    Ok(Some(match block {
        AssistantContentBlock::Text { text } => ResponseContentBlock::Text { text },
        AssistantContentBlock::Reasoning { text } => {
            ResponseContentBlock::Thinking { thinking: text }
        }
        AssistantContentBlock::ToolCall(call) => {
            // Collected tool input must be an object; partial bytes remain available only in SSE.
            let Some(input) = tool_input(&call.arguments, truncated)? else {
                return Ok(None);
            };
            ResponseContentBlock::ToolUse {
                id: call.id,
                name: call.name,
                input,
            }
        }
    }))
}

/// Parses tool input, distinguishing budget-truncated JSON from malformed or non-object input.
fn tool_input(arguments: &str, truncated: bool) -> Result<Option<Value>, AnthropicApiError> {
    match serde_json::from_str::<Value>(arguments) {
        Ok(value) if value.is_object() => Ok(Some(value)),
        Err(error) if truncated && error.is_eof() => Ok(None),
        _ => Err(AnthropicApiError::stream()),
    }
}

/// Maps successful terminal causes; aborted or invalid output is a protocol error.
fn stop(
    reason: FinishReason,
    has_tools: bool,
) -> Result<(StopReason, Option<String>), AnthropicApiError> {
    match reason {
        FinishReason::Length => Ok((StopReason::MaxTokens, None)),
        FinishReason::Stop(Some(EngineStopReason::Text(text))) => {
            Ok((StopReason::StopSequence, Some(text)))
        }
        FinishReason::Stop(_) if has_tools => Ok((StopReason::ToolUse, None)),
        FinishReason::Stop(_) => Ok((StopReason::EndTurn, None)),
        _ => Err(AnthropicApiError::stream()),
    }
}

/// Uses the canonical generation counts; cache reads are separated from uncached input.
fn token_usage(prompt: usize, output: usize, cached: usize) -> AnthropicUsage {
    AnthropicUsage {
        input_tokens: prompt.saturating_sub(cached),
        output_tokens: output,
        cache_read_input_tokens: cached,
    }
}

/// Serializes protocol-owned JSON values to the named Anthropic SSE event.
fn sse(name: &'static str, body: Value) -> Event {
    Event::default().event(name).data(body.to_string())
}
