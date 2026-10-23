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
use futures::future::{BoxFuture, ready};
use futures::stream::{BoxStream, FuturesUnordered, select_all};
use futures::{FutureExt, StreamExt};
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

/// Deferred backend initialization retaining one candidate's existing admission ownership.
pub(crate) type PendingCompletion = BoxFuture<'static, Result<Generated, GenerationError>>;

/// Formatting and selection rules shared by all candidates of one completion request.
#[derive(Clone, Copy)]
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

struct CollectedCompletion {
    index: usize,
    score: f32,
    prompt_ids: Vec<u32>,
    choice: CompletionChoice,
    usage: Usage,
}

/// A candidate retains text offsets; the response owner aggregates terminal usage across candidates.
struct CompletionChunk {
    choice: CompletionStreamChoice,
    usage: Option<Usage>,
    prompt_token_ids: Option<Vec<Vec<u32>>>,
}

/// Supervises every candidate through initialization and collection, then ranks each prompt's results.
/// Completion order never changes choice indexes or the tie order used by best-of selection.
pub(crate) async fn text_collected_many(
    first: Generated,
    pending: Vec<PendingCompletion>,
    idle: Duration,
    options: CompletionResponseOptions,
) -> Response {
    let metadata = ResponseMetadata::from_generated(&first);
    let mut candidates = [ready(Ok(first)).boxed()]
        .into_iter()
        .chain(pending)
        .enumerate()
        .map(|(index, pending)| collect_completion(index, pending, idle, options))
        .collect::<FuturesUnordered<_>>();
    let mut completed = Vec::new();
    while let Some(result) = candidates.next().await {
        match result {
            Ok(candidate) => completed.push(candidate),
            Err(error) => return openai_error(error),
        }
    }
    completed.sort_by_key(|candidate| candidate.index);

    let mut groups = Vec::new();
    let mut prompt_token_ids = Vec::new();
    let mut total_prompt_tokens = 0;
    let mut total_completion_tokens = 0;
    let mut total_cached_tokens = 0;
    for candidate in completed {
        total_prompt_tokens += candidate.usage.prompt_tokens;
        total_completion_tokens += candidate.usage.completion_tokens;
        total_cached_tokens += candidate.usage.prompt_tokens_details.cached_tokens;
        if candidate.index % options.candidates_per_prompt == 0 {
            prompt_token_ids.push(candidate.prompt_ids);
            groups.push(Vec::new());
        }
        groups
            .last_mut()
            .unwrap()
            .push((candidate.score, candidate.choice));
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

/// Interleaves bounded candidates while preserving each choice's index and text order.
/// A failure drops all initialization and output futures before its SSE error is exposed.
pub(crate) fn text_stream_many(
    first: Generated,
    pending: Vec<PendingCompletion>,
    idle: Duration,
    include_usage: bool,
    options: CompletionResponseOptions,
) -> Response {
    let metadata = ResponseMetadata::from_generated(&first);
    let mut candidates = select_all(
        [ready(Ok(first)).boxed()]
            .into_iter()
            .chain(pending)
            .enumerate()
            .map(|(index, pending)| stream_completion(index, pending, idle, options)),
    );
    let events = async_stream::stream! {
        let mut total_prompt_tokens = 0;
        let mut total_completion_tokens = 0;
        let mut total_cached_tokens = 0;
        while let Some(result) = candidates.next().await {
            let chunk = match result {
                Ok(chunk) => chunk,
                Err(_) => {
                    // The client may stop polling after the error, so cancellation precedes yield.
                    drop(candidates);
                    yield Ok::<_, Infallible>(Event::default().json_data(stream_backend_error()).unwrap());
                    return;
                }
            };
            if let Some(usage) = chunk.usage {
                total_prompt_tokens += usage.prompt_tokens;
                total_completion_tokens += usage.completion_tokens;
                total_cached_tokens += usage.prompt_tokens_details.cached_tokens;
            }
            yield Ok::<_, Infallible>(Event::default().json_data(CompletionStreamResponse {
                metadata: metadata.clone(), object: "text_completion",
                choices: vec![chunk.choice], usage: None,
                prompt_token_ids: chunk.prompt_token_ids,
            }).unwrap());
        }
        if include_usage {
            yield Ok::<_, Infallible>(Event::default().json_data(CompletionStreamResponse { metadata, object: "text_completion", choices: Vec::new(), usage: Some(Usage::from_counts(total_prompt_tokens, total_completion_tokens, total_cached_tokens)), prompt_token_ids: None }).unwrap());
        }
        yield Ok(Event::default().data("[DONE]"));
    };
    sse_response(events)
}

/// Owns one candidate from submission through upstream collection and completion formatting.
async fn collect_completion(
    index: usize,
    pending: PendingCompletion,
    idle: Duration,
    options: CompletionResponseOptions,
) -> Result<CollectedCompletion, GenerationError> {
    let generated = pending.await?;
    let prompt_ids = generated
        .routed
        .routed_request
        .request
        .prompt_token_ids
        .clone();
    let tokenizer = generated.tokenizer.clone();
    let prompt_text = if options.echo {
        tokenizer
            .decode(&prompt_ids, false)
            .map_err(|_| GenerationError::Internal)?
    } else {
        String::new()
    };
    let mut output = idle_timed(decoded(generated), idle)
        .collect_output()
        .await
        .map_err(|_| GenerationError::RequestFailed)?;
    let finish_reason = completion_finish_reason(&output.finish_reason)
        .map_err(|_| GenerationError::RequestFailed)?;
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
        let prompt = output
            .prompt_logprobs
            .ok_or(GenerationError::BackendProtocol)?;
        let mut echoed = completion_prompt_logprobs(
            &prompt_ids,
            prompt,
            &tokenizer,
            options.return_tokens_as_token_ids,
        )?;
        if let Some(generated) = logprobs {
            echoed.text_offset.extend(generated.text_offset);
            echoed.token_logprobs.extend(generated.token_logprobs);
            echoed.tokens.extend(generated.tokens);
            echoed.top_logprobs.extend(generated.top_logprobs);
        }
        logprobs = Some(echoed);
    }
    Ok(CollectedCompletion {
        index,
        score,
        prompt_ids,
        choice: CompletionChoice {
            index: 0,
            text: format!("{prompt_text}{}", output.text),
            logprobs,
            token_ids: options.return_token_ids.then_some(output.token_ids),
            finish_reason: finish_reason.to_owned(),
            stop_reason: openai_stop_reason(&output.finish_reason),
        },
        usage: Usage::from_counts(
            output.usage.prompt_token_count,
            output.usage.output_token_count,
            output.usage.cached_token_count,
        ),
    })
}

/// Retains one candidate's initialization, tokenizer and offset until its terminal output or cancellation.
fn stream_completion(
    index: usize,
    pending: PendingCompletion,
    idle: Duration,
    options: CompletionResponseOptions,
) -> BoxStream<'static, Result<CompletionChunk, GenerationError>> {
    async_stream::try_stream! {
        let generated = pending.await?;
        let tokenizer = generated.tokenizer.clone();
        let prompt_ids = &generated.routed.routed_request.request.prompt_token_ids;
        // Streaming accepts one prompt. Candidate zero publishes its prompt IDs.
        let prompt_token_ids = (index == 0 && options.return_prompt_token_ids)
            .then(|| vec![prompt_ids.clone()]);
        let prompt_text = if options.echo {
            tokenizer.decode(prompt_ids, false).map_err(|_| GenerationError::Internal)?
        } else {
            String::new()
        };
        let mut stream = Box::pin(idle_timed(decoded(generated), idle));
        let mut text_offset = 0;
        while let Some(event) = stream.next().await {
            match event.map_err(|_| GenerationError::RequestFailed)? {
                DecodedTextEvent::Start { prompt_token_ids: ids, prompt_logprobs } => {
                    if options.echo {
                        let logprobs = if options.expose_logprobs {
                            Some(completion_prompt_logprobs(
                                &ids,
                                prompt_logprobs.ok_or(GenerationError::BackendProtocol)?,
                                &tokenizer,
                                options.return_tokens_as_token_ids,
                            )?)
                        } else {
                            None
                        };
                        text_offset = prompt_text.chars().count();
                        yield CompletionChunk {
                            choice: CompletionStreamChoice {
                                index: index as u32, text: prompt_text.clone(), logprobs,
                                ..Default::default()
                            },
                            usage: None,
                            prompt_token_ids: prompt_token_ids.clone(),
                        };
                    }
                }
                DecodedTextEvent::TextDelta { delta, token_ids, logprobs, finished } => {
                    let delta = delta.text;
                    let logprobs = completion_logprobs(
                        &token_ids, logprobs, text_offset, options.return_tokens_as_token_ids,
                    );
                    text_offset += delta.chars().count();
                    if !options.echo_without_generation && (!delta.is_empty() || logprobs.is_some()) {
                        yield CompletionChunk {
                            choice: CompletionStreamChoice {
                                index: index as u32, text: delta, logprobs,
                                token_ids: options.return_token_ids.then_some(token_ids),
                                finish_reason: None, stop_reason: None,
                            },
                            usage: None,
                            prompt_token_ids: prompt_token_ids.clone(),
                        };
                    }
                    if let Some(finished) = finished {
                        let finish_reason = completion_finish_reason(&finished.finish_reason)
                            .map_err(|_| GenerationError::RequestFailed)?;
                        yield CompletionChunk {
                            choice: CompletionStreamChoice {
                                index: index as u32, finish_reason: Some(finish_reason),
                                stop_reason: openai_stop_reason(&finished.finish_reason),
                                ..Default::default()
                            },
                            usage: Some(Usage::from_counts(
                                finished.usage.prompt_token_count,
                                finished.usage.output_token_count,
                                finished.usage.cached_token_count,
                            )),
                            prompt_token_ids: prompt_token_ids.clone(),
                        };
                        break;
                    }
                }
            }
        }
    }.boxed()
}

/// Encodes sampled and top-candidate scores for completion responses with character-based offsets.
/// Echoed prompts supply the initial offset; missing scores retain their public null representation.
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

fn completion_finish_reason(finish_reason: &FinishReason) -> Result<&'static str, ()> {
    match finish_reason {
        FinishReason::Stop(_) => Ok("stop"),
        FinishReason::Length => Ok("length"),
        FinishReason::Abort => Ok("abort"),
        FinishReason::Repetition(_) => Ok("repetition"),
        FinishReason::Error => Err(()),
    }
}
