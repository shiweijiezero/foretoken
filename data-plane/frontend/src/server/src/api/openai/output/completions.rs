// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Encodes text completion fan-out, echoed prompt probabilities and generated output as JSON or SSE.

use std::collections::BTreeMap;
use std::convert::Infallible;
use std::time::Duration;

use axum::Json;
use axum::response::sse::Event;
use axum::response::{IntoResponse, Response};
use foretoken_chat::FinishReason;
use foretoken_text::{
    DecodedLogprobs, DecodedPromptLogprobs, DecodedTextEvent, TextOutputStreamExt,
};
use futures::StreamExt;
use serde::Serialize;

use super::super::openai_error;
use super::{
    OpenAiStopReason, ResponseMetadata, Usage, openai_stop_reason, selected_logprob,
    stream_backend_error,
};
use crate::api::stream::{decoded, idle_timed, sse_response};
use crate::runtime::{Generated, GenerationError};

#[derive(Serialize)]
struct CompletionLogprobs {
    text_offset: Vec<usize>,
    token_logprobs: Vec<Option<f32>>,
    tokens: Vec<String>,
    top_logprobs: Vec<Option<BTreeMap<String, f32>>>,
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

fn logprob_token_name(
    entry: &foretoken_text::DecodedTokenLogprob,
    return_as_token_id: bool,
) -> String {
    if return_as_token_id {
        format!("token_id:{}", entry.token_id)
    } else {
        entry.token.clone()
    }
}

fn completion_logprobs(
    token_ids: &[u32],
    logprobs: Option<DecodedLogprobs>,
    initial_text_offset: usize,
    return_as_token_id: bool,
) -> Option<CompletionLogprobs> {
    let logprobs = logprobs?;
    let mut text_offset = Vec::with_capacity(token_ids.len());
    let mut token_logprobs = Vec::with_capacity(token_ids.len());
    let mut tokens = Vec::with_capacity(token_ids.len());
    let mut top_logprobs = Vec::with_capacity(token_ids.len());
    let mut offset = initial_text_offset;

    for (token_id, position) in token_ids.iter().copied().zip(logprobs.positions) {
        let selected_entry = selected_logprob(&position, token_id);
        let selected = selected_entry.map(|entry| {
            (
                logprob_token_name(entry, return_as_token_id),
                entry.logprob.max(-9999.0),
            )
        });
        let token = selected.as_ref().map_or_else(
            || format!("token_id:{token_id}"),
            |(token, _)| token.clone(),
        );
        let top = position
            .entries
            .iter()
            .map(|entry| {
                (
                    logprob_token_name(entry, return_as_token_id),
                    entry.logprob.max(-9999.0),
                )
            })
            .collect::<BTreeMap<_, _>>();
        text_offset.push(offset);
        offset += selected_entry.map_or_else(
            || token.chars().count(),
            |entry| entry.token.chars().count(),
        );
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

/// Encode echoed prompt positions, preserving the unscored first token as null.
fn completion_prompt_logprobs(
    token_ids: &[u32],
    prompt: DecodedPromptLogprobs,
    tokenizer: &foretoken_tokenizer::DynTokenizer,
    return_as_token_id: bool,
) -> Result<CompletionLogprobs, GenerationError> {
    // Echo retains an initial BOS marker even when generation skips special tokens.
    let first_token = tokenizer
        .decode(&[prompt.first_token_id], false)
        .map_err(|_| GenerationError::Internal)?;
    let mut logprobs = completion_logprobs(
        &token_ids[1..],
        Some(DecodedLogprobs {
            positions: prompt.scored_positions,
        }),
        first_token.chars().count(),
        return_as_token_id,
    )
    .unwrap();
    logprobs.text_offset.insert(0, 0);
    logprobs.token_logprobs.insert(0, None);
    logprobs.tokens.insert(
        0,
        if return_as_token_id {
            format!("token_id:{}", prompt.first_token_id)
        } else {
            first_token
        },
    );
    logprobs.top_logprobs.insert(0, None);
    Ok(logprobs)
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

pub(crate) struct CompletionResponseOptions {
    pub n: usize,
    pub candidates_per_prompt: usize,
    pub echo: bool,
    pub echo_without_generation: bool,
    pub expose_logprobs: bool,
    pub return_token_ids: bool,
    pub return_tokens_as_token_ids: bool,
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
        let tokenizer = item.tokenizer.clone();
        let prompt_text = if options.echo {
            match tokenizer.decode(&prompt_ids, false) {
                Ok(prompt) => prompt,
                Err(_) => return openai_error(GenerationError::Internal),
            }
        } else {
            String::new()
        };
        match idle_timed(decoded(item), idle).collect_output().await {
            Ok(mut output) => {
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
                // A zero-output echo still runs one backend decode step to obtain prompt scores.
                if options.echo_without_generation {
                    output.text.clear();
                    output.token_ids.clear();
                    output.logprobs = None;
                }
                let mut logprobs = if options.expose_logprobs {
                    completion_logprobs(
                        &output.token_ids,
                        output.logprobs,
                        prompt_text.chars().count(),
                        options.return_tokens_as_token_ids,
                    )
                } else {
                    None
                };
                if options.echo && options.expose_logprobs {
                    let Some(prompt) = output.prompt_logprobs else {
                        return openai_error(GenerationError::BackendProtocol);
                    };
                    let mut echoed = match completion_prompt_logprobs(
                        &prompt_ids,
                        prompt,
                        &tokenizer,
                        options.return_tokens_as_token_ids,
                    ) {
                        Ok(logprobs) => logprobs,
                        Err(error) => return openai_error(error),
                    };
                    if let Some(generated) = logprobs {
                        echoed.text_offset.extend(generated.text_offset);
                        echoed.token_logprobs.extend(generated.token_logprobs);
                        echoed.tokens.extend(generated.tokens);
                        echoed.top_logprobs.extend(generated.top_logprobs);
                    }
                    logprobs = Some(echoed);
                }
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
                        logprobs,
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
    options: CompletionResponseOptions,
) -> Response {
    let Some(first) = generated.first() else {
        return openai_error(GenerationError::InvalidRequest);
    };
    let metadata = ResponseMetadata::from_generated(first);
    // The HTTP layer admits streaming only for one prompt, irrespective of `n`.
    let prompt_token_ids = options
        .return_prompt_token_ids
        .then(|| vec![first.routed.routed_request.request.prompt_token_ids.clone()]);
    let events = async_stream::stream! {
        let mut total_prompt_tokens = 0;
        let mut total_completion_tokens = 0;
        let mut total_cached_tokens = 0;
        for (index, generated) in generated.into_iter().enumerate() {
            let tokenizer = generated.tokenizer.clone();
            let prompt_text = if options.echo {
                match tokenizer.decode(
                    &generated.routed.routed_request.request.prompt_token_ids,
                    false,
                ) {
                    Ok(text) => text,
                    Err(_) => {
                        yield Ok::<_, Infallible>(Event::default().json_data(stream_backend_error()).unwrap());
                        break;
                    }
                }
            } else {
                String::new()
            };
            let mut stream = Box::pin(idle_timed(decoded(generated), idle));
            let mut text_offset = 0;
            while let Some(event) = stream.next().await {
                match event {
                    Ok(DecodedTextEvent::Start { prompt_token_ids: ids, prompt_logprobs }) => {
                        if options.echo {
                            let logprobs = if options.expose_logprobs {
                                let Some(prompt) = prompt_logprobs else {
                                    yield Ok(Event::default().json_data(stream_backend_error()).unwrap());
                                    break;
                                };
                                match completion_prompt_logprobs(
                                    &ids, prompt, &tokenizer, options.return_tokens_as_token_ids,
                                ) {
                                    Ok(logprobs) => Some(logprobs),
                                    Err(_) => {
                                        yield Ok(Event::default().json_data(stream_backend_error()).unwrap());
                                        break;
                                    }
                                }
                            } else {
                                None
                            };
                            text_offset = prompt_text.chars().count();
                            yield Ok(Event::default().json_data(CompletionStreamResponse {
                                metadata: metadata.clone(), object: "text_completion",
                                choices: vec![CompletionStreamChoice { index: index as u32, text: prompt_text.clone(), logprobs, finish_reason: None, ..Default::default() }], usage: None,
                                prompt_token_ids: (index == 0).then(|| prompt_token_ids.clone()).flatten(),
                            }).unwrap());
                        }
                    }
                    Ok(DecodedTextEvent::TextDelta { delta, token_ids, logprobs, finished }) => {
                        let logprobs = completion_logprobs(
                            &token_ids,
                            logprobs,
                            text_offset,
                            options.return_tokens_as_token_ids,
                        );
                        text_offset += delta.chars().count();
                        if !options.echo_without_generation && (!delta.is_empty() || logprobs.is_some()) {
                            yield Ok::<_, Infallible>(Event::default().json_data(CompletionStreamResponse {
                                metadata: metadata.clone(), object: "text_completion",
                                choices: vec![CompletionStreamChoice { index: index as u32, text: delta, logprobs, token_ids: options.return_token_ids.then_some(token_ids), finish_reason: None, stop_reason: None }], usage: None,
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
    sse_response(events)
}
