// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Adapts the decoded output stream into streaming and collected HTTP responses.

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::Json;
use axum::response::sse::{Event, Sse};
use axum::response::{IntoResponse, Response};
use foretoken_chat::{AssistantBlockKind, AssistantMessageExt as _, ChatEvent, FinishReason};
use foretoken_engine_core_client::protocol::output::StopReason;
use foretoken_text::output::decoded_text_event_stream;
use foretoken_text::{DecodedLogprobs, DecodedPositionLogprobs};
use foretoken_text::{DecodedTextEvent, TextOutputStreamExt};
use futures::StreamExt;
use serde::Serialize;
use vllm_llm::FinishReason as VllmFinishReason;

use crate::http::openai_error;
use crate::runtime::{Generated, GeneratedChat, GenerationError};

#[derive(Clone, Serialize)]
struct ResponseMetadata {
    id: String,
    created: u64,
    model: String,
}

impl ResponseMetadata {
    fn from_generated(generated: &Generated) -> Self {
        Self {
            id: generated.routed.routed_request.request.request_id.clone(),
            created: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_secs(),
            model: generated.routed.routed_request.decision.model.clone(),
        }
    }
}

#[derive(Serialize)]
struct Usage {
    prompt_tokens: usize,
    completion_tokens: usize,
    total_tokens: usize,
    prompt_tokens_details: PromptTokenUsageDetails,
}

#[derive(Serialize)]
struct PromptTokenUsageDetails {
    cached_tokens: usize,
}

impl Usage {
    fn from_counts(prompt_tokens: usize, completion_tokens: usize, cached_tokens: usize) -> Self {
        Self {
            prompt_tokens,
            completion_tokens,
            total_tokens: prompt_tokens + completion_tokens,
            prompt_tokens_details: PromptTokenUsageDetails { cached_tokens },
        }
    }
}

#[derive(Serialize)]
struct CompletionLogprobs {
    text_offset: Vec<usize>,
    token_logprobs: Vec<Option<f32>>,
    tokens: Vec<String>,
    top_logprobs: Vec<Option<BTreeMap<String, f32>>>,
}

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

#[derive(Serialize)]
#[serde(untagged)]
enum OpenAiStopReason {
    TokenId(u32),
    Text(String),
}

#[derive(Serialize)]
struct CompletionChoice {
    index: u32,
    text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    logprobs: Option<CompletionLogprobs>,
    #[serde(skip_serializing_if = "Option::is_none")]
    token_ids: Option<Vec<u32>>,
    finish_reason: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    stop_reason: Option<OpenAiStopReason>,
}

#[derive(Serialize)]
struct CompletionResponse {
    #[serde(flatten)]
    metadata: ResponseMetadata,
    object: &'static str,
    choices: Vec<CompletionChoice>,
    usage: Usage,
    #[serde(skip_serializing_if = "Option::is_none")]
    prompt_token_ids: Option<Vec<Vec<u32>>>,
}

#[derive(Default, Serialize)]
struct CompletionStreamChoice {
    index: u32,
    text: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    logprobs: Option<CompletionLogprobs>,
    #[serde(skip_serializing_if = "Option::is_none")]
    token_ids: Option<Vec<u32>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    finish_reason: Option<&'static str>,
    #[serde(skip_serializing_if = "Option::is_none")]
    stop_reason: Option<OpenAiStopReason>,
}

#[derive(Serialize)]
struct CompletionStreamResponse {
    #[serde(flatten)]
    metadata: ResponseMetadata,
    object: &'static str,
    choices: Vec<CompletionStreamChoice>,
    #[serde(skip_serializing_if = "Option::is_none")]
    usage: Option<Usage>,
    #[serde(skip_serializing_if = "Option::is_none")]
    prompt_token_ids: Option<Vec<Vec<u32>>>,
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

#[derive(Serialize)]
struct StreamError<'a> {
    error: StreamErrorBody<'a>,
}

#[derive(Serialize)]
struct StreamErrorBody<'a> {
    message: &'a str,
    #[serde(rename = "type")]
    kind: &'a str,
    code: &'a str,
}

fn stream_backend_error() -> StreamError<'static> {
    StreamError {
        error: StreamErrorBody {
            message: "model server request failed",
            kind: "server_error",
            code: "request_failed",
        },
    }
}

fn openai_stop_reason(finish_reason: &VllmFinishReason) -> Option<OpenAiStopReason> {
    match finish_reason.as_stop_reason()? {
        StopReason::TokenId(token_id) => Some(OpenAiStopReason::TokenId(*token_id)),
        StopReason::Text(text) => Some(OpenAiStopReason::Text(text.clone())),
    }
}

fn selected_logprob(
    position: &DecodedPositionLogprobs,
    token_id: u32,
) -> Option<&foretoken_text::DecodedTokenLogprob> {
    position
        .entries
        .iter()
        .find(|entry| entry.token_id == token_id)
}

fn completion_logprobs(
    token_ids: &[u32],
    logprobs: Option<DecodedLogprobs>,
    initial_text_offset: usize,
) -> Option<CompletionLogprobs> {
    let logprobs = logprobs?;
    let mut text_offset = Vec::with_capacity(token_ids.len());
    let mut token_logprobs = Vec::with_capacity(token_ids.len());
    let mut tokens = Vec::with_capacity(token_ids.len());
    let mut top_logprobs = Vec::with_capacity(token_ids.len());
    let mut offset = initial_text_offset;

    for (token_id, position) in token_ids.iter().copied().zip(logprobs.positions) {
        let selected = selected_logprob(&position, token_id)
            .map(|entry| (entry.token.clone(), entry.logprob.max(-9999.0)));
        let token = selected.as_ref().map_or_else(
            || format!("token_id:{token_id}"),
            |(token, _)| token.clone(),
        );
        let top = position
            .entries
            .into_iter()
            .map(|entry| (entry.token, entry.logprob.max(-9999.0)))
            .collect::<BTreeMap<_, _>>();
        text_offset.push(offset);
        offset += token.len();
        token_logprobs.push(selected.map(|(_, logprob)| logprob));
        tokens.push(token);
        top_logprobs.push(Some(top));
    }

    Some(CompletionLogprobs {
        text_offset,
        token_logprobs,
        tokens,
        top_logprobs,
    })
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

fn decoded(generated: Generated) -> impl foretoken_text::TextOutputStream {
    let request_id = generated.routed.routed_request.request.request_id.clone();
    let mut stream = generated.routed.stream;
    let raw = async_stream::stream! {
        while let Some(item) = stream.next().await {
            match item {
                Ok(output) => yield Ok(output),
                Err(_) => break,
            }
        }
    };
    decoded_text_event_stream(
        request_id,
        generated.tokenizer,
        Box::pin(raw),
        generated.decode_options,
        true,
    )
}

pub(crate) fn idle_timed(
    stream: impl foretoken_text::TextOutputStream,
    idle: Duration,
) -> impl foretoken_text::TextOutputStream {
    async_stream::stream! {
        let mut stream = Box::pin(stream);
        loop {
            match tokio::time::timeout(idle, stream.next()).await {
                Ok(Some(event)) => yield event,
                Ok(None) => break,
                Err(_) => {
                    // Dropping the output stream after this item aborts the backend request.
                    yield Err(foretoken_text::Error::StreamClosedBeforeTerminalOutput {
                        request_id: "idle-timeout".into(),
                    });
                    break;
                }
            }
        }
    }
}

fn chat_events(
    generated: GeneratedChat,
    idle: Duration,
) -> foretoken_chat::Result<(
    bool,
    impl futures::Stream<Item = foretoken_chat::Result<ChatEvent>> + Send,
)> {
    let GeneratedChat {
        generated,
        output_processor,
        include_reasoning,
    } = generated;
    let decoded = idle_timed(decoded(generated), idle)
        .map(|event| event.map_err(foretoken_chat::Error::from));
    output_processor
        .process(Box::pin(decoded))
        .map(|stream| (include_reasoning, stream))
}

fn completion_finish_reason(finish_reason: &FinishReason) -> Result<&'static str, ()> {
    match finish_reason {
        FinishReason::Stop(_) => Ok("stop"),
        FinishReason::Length => Ok("length"),
        FinishReason::Abort => Ok("abort"),
        FinishReason::Repetition(_) => Ok("repetition"),
        FinishReason::Error => Err(()),
    }
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
    Sse::new(events).into_response()
}

pub(crate) struct CompletionResponseOptions {
    pub n: usize,
    pub candidates_per_prompt: usize,
    pub echo: bool,
    pub expose_logprobs: bool,
    pub return_token_ids: bool,
    pub return_prompt_token_ids: bool,
}

/// Collects a bounded completion fan-out, ranks each `best_of` group, and assigns OpenAI's
/// globally stable choice indexes (prompt order, then selected candidate order).
pub(crate) async fn text_collected_many(
    generated: Vec<Generated>,
    idle: Duration,
    options: CompletionResponseOptions,
) -> Response {
    let Some(first) = generated.first() else {
        return openai_error(GenerationError::InvalidRequest);
    };
    let metadata = ResponseMetadata::from_generated(first);
    let mut groups = Vec::new();
    let mut prompt_token_ids = Vec::new();
    let mut total_prompt_tokens = 0;
    let mut total_completion_tokens = 0;
    let mut total_cached_tokens = 0;

    for (candidate_index, item) in generated.into_iter().enumerate() {
        let prompt_ids = item.routed.routed_request.request.prompt_token_ids.clone();
        let prompt_text = if options.echo {
            match item.tokenizer.decode(&prompt_ids, false) {
                Ok(prompt) => prompt,
                Err(_) => return openai_error(GenerationError::Internal),
            }
        } else {
            String::new()
        };
        match idle_timed(decoded(item), idle).collect_output().await {
            Ok(output) => {
                let Ok(finish_reason) = completion_finish_reason(&output.finish_reason) else {
                    return openai_error(GenerationError::RequestFailed);
                };
                let score = output
                    .logprobs
                    .as_ref()
                    .map_or(f32::NEG_INFINITY, |logprobs| {
                        output
                            .token_ids
                            .iter()
                            .zip(&logprobs.positions)
                            .filter_map(|(id, position)| {
                                selected_logprob(position, *id).map(|entry| entry.logprob)
                            })
                            .sum()
                    });
                total_prompt_tokens += output.usage.prompt_token_count;
                total_completion_tokens += output.usage.output_token_count;
                total_cached_tokens += output.usage.cached_token_count;
                if candidate_index % options.candidates_per_prompt == 0 {
                    prompt_token_ids.push(prompt_ids);
                    groups.push(Vec::new());
                }
                groups.last_mut().unwrap().push((
                    score,
                    CompletionChoice {
                        index: 0,
                        text: format!("{prompt_text}{}", output.text),
                        logprobs: options
                            .expose_logprobs
                            .then(|| {
                                completion_logprobs(
                                    &output.token_ids,
                                    output.logprobs,
                                    prompt_text.len(),
                                )
                            })
                            .flatten(),
                        token_ids: options.return_token_ids.then_some(output.token_ids),
                        finish_reason: finish_reason.to_owned(),
                        stop_reason: openai_stop_reason(&output.finish_reason),
                    },
                ));
            }
            Err(_) => return openai_error(GenerationError::RequestFailed),
        }
    }
    let mut choices = Vec::with_capacity(groups.len() * options.n);
    for group in &mut groups {
        group.sort_by(|left, right| right.0.total_cmp(&left.0));
        for (_, mut choice) in group.drain(..options.n) {
            choice.index = choices.len() as u32;
            choices.push(choice);
        }
    }
    Json(CompletionResponse {
        metadata,
        object: "text_completion",
        choices,
        usage: Usage::from_counts(
            total_prompt_tokens,
            total_completion_tokens,
            total_cached_tokens,
        ),
        prompt_token_ids: options.return_prompt_token_ids.then_some(prompt_token_ids),
    })
    .into_response()
}

/// Streams each bounded `n` candidate in request order. This preserves stable choice indexes
/// without exposing backend request identities or transfer parameters.
pub(crate) fn text_stream_many(
    generated: Vec<Generated>,
    idle: Duration,
    include_usage: bool,
    return_token_ids: bool,
    return_prompt_token_ids: bool,
) -> Response {
    let Some(first) = generated.first() else {
        return openai_error(GenerationError::InvalidRequest);
    };
    let metadata = ResponseMetadata::from_generated(first);
    // The HTTP layer admits streaming only for one prompt, irrespective of `n`.
    let prompt_token_ids = return_prompt_token_ids
        .then(|| vec![first.routed.routed_request.request.prompt_token_ids.clone()]);
    let events = async_stream::stream! {
        let mut total_prompt_tokens = 0;
        let mut total_completion_tokens = 0;
        let mut total_cached_tokens = 0;
        for (index, generated) in generated.into_iter().enumerate() {
            let mut stream = Box::pin(idle_timed(decoded(generated), idle));
            let mut text_offset = 0;
            while let Some(event) = stream.next().await {
                match event {
                    Ok(DecodedTextEvent::Start { .. }) => {}
                    Ok(DecodedTextEvent::TextDelta { delta, token_ids, logprobs, finished }) => {
                        let logprobs = completion_logprobs(&token_ids, logprobs, text_offset);
                        text_offset += delta.len();
                        if !delta.is_empty() || logprobs.is_some() {
                            yield Ok::<_, Infallible>(Event::default().json_data(CompletionStreamResponse {
                                metadata: metadata.clone(), object: "text_completion",
                                choices: vec![CompletionStreamChoice { index: index as u32, text: delta, logprobs, token_ids: return_token_ids.then_some(token_ids), finish_reason: None, stop_reason: None }], usage: None,
                                prompt_token_ids: (index == 0).then(|| prompt_token_ids.clone()).flatten(),
                            }).unwrap());
                        }
                        if let Some(finished) = finished {
                            let Ok(finish_reason) = completion_finish_reason(&finished.finish_reason) else {
                                yield Ok(Event::default().json_data(stream_backend_error()).unwrap());
                                break;
                            };
                            total_prompt_tokens += finished.usage.prompt_token_count;
                            total_completion_tokens += finished.usage.output_token_count;
                            total_cached_tokens += finished.usage.cached_token_count;
                            yield Ok(Event::default().json_data(CompletionStreamResponse {
                                metadata: metadata.clone(), object: "text_completion",
                                choices: vec![CompletionStreamChoice { index: index as u32, text: String::new(), logprobs: None, token_ids: None, finish_reason: Some(finish_reason), stop_reason: openai_stop_reason(&finished.finish_reason) }], usage: None,
                                prompt_token_ids: (index == 0).then(|| prompt_token_ids.clone()).flatten(),
                            }).unwrap());
                            break;
                        }
                    }
                    Err(_) => { yield Ok(Event::default().json_data(stream_backend_error()).unwrap()); break; }
                }
            }
        }
        if include_usage {
            yield Ok::<_, Infallible>(Event::default().json_data(CompletionStreamResponse { metadata, object: "text_completion", choices: Vec::new(), usage: Some(Usage::from_counts(total_prompt_tokens, total_completion_tokens, total_cached_tokens)), prompt_token_ids: None }).unwrap());
        }
        yield Ok(Event::default().data("[DONE]"));
    };
    Sse::new(events).into_response()
}

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
