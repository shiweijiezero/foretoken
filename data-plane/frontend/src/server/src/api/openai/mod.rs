// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! OpenAI-compatible HTTP handlers over the shared generation pipeline.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::rejection::JsonRejection;
use axum::extract::{Path as AxumPath, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use foretoken_admission::{AdmissionRequest, CallerIdentity, with_caller};
use foretoken_chat::{
    ChatOptions, ChatRequest, GenerationPromptMode, ParserSelection, ResolvedToolContext,
};
use foretoken_text::{Prompt, SamplingParams, TextDecodeOptions};
use futures::FutureExt;
use hyproxy::headers::hopby::StripHopByHopLayer;
use serde_json::{Value, json};
use tracing::Instrument;

mod async_video;
mod convert;
mod output;
mod stored;
mod types;
mod video;

use self::convert::{
    completion_sampling, decode_options, openai_message, openai_tool, prepare_chat,
    validate_multimodal_input,
};
use self::output::{
    CompletionResponseOptions, chat_collected, chat_stream_with_options, text_collected_many,
    text_stream_many,
};
use self::types::{
    ChatCompletionRequest, CompletionRequest, DetokenizeRequest, DetokenizeResponse,
    TokenizeRequest, TokenizeResponse, completion_prompts,
};
use super::{ApiState, RequestTiming, server_request_id};
use crate::AdmissionOrigin;
use crate::runtime::{GenerationError, GenerationRequest, RequestSnapshot};

const MAX_COMPLETION_FAN_OUT: usize = 64;

/// Registers OpenAI generation, model discovery, and tokenization endpoints.
pub(super) fn router() -> Router<ApiState> {
    Router::new()
        .route("/v1/models", get(list_models))
        .route("/v1/models/{model}", get(retrieve_model))
        .route("/tokenize", post(tokenize))
        .route("/detokenize", post(detokenize))
        .route("/v1/generate", post(completions))
        .route("/v1/completions", post(completions))
        .route("/v1/chat/completions", post(chat_completions))
        .route(
            "/v1/videos/sync",
            post(video::generate).layer(StripHopByHopLayer::new()),
        )
        .merge(async_video::router())
        .merge(stored::router())
}

/// Admits a bounded completion batch and returns shared generation as OpenAI JSON or SSE.
async fn completions(
    Extension(state): Extension<RequestSnapshot>,
    timing: Option<Extension<RequestTiming>>,
    request: Result<Json<CompletionRequest>, JsonRejection>,
) -> Response {
    // Protected requests inherit HTTP intake time; legacy callers retain handler entry.
    // Every candidate shares the same admission, preprocessing, and completion budget.
    let timing = timing.map_or_else(RequestTiming::now, |Extension(timing)| timing);
    let Json(request) = match request {
        Ok(request) => request,
        Err(error) => return json_error(error),
    };
    let stream = request.stream;
    let include_usage = request.stream_options.include_usage;
    let mut sampling_params = completion_sampling(&request);
    let prompts = completion_prompts(request.prompt);
    let best_of = request.best_of.unwrap_or(request.n);
    if include_usage && !stream {
        return client_error("stream_options.include_usage requires stream=true");
    }
    if request.n == 0 {
        return client_error("n must be at least 1");
    }
    if best_of < request.n {
        return client_error("best_of must be greater than or equal to n");
    }
    if prompts.len().saturating_mul(best_of as usize) > MAX_COMPLETION_FAN_OUT {
        return client_error(format!(
            "prompt count multiplied by best_of cannot exceed {MAX_COMPLETION_FAN_OUT}"
        ));
    }
    if stream && prompts.len() != 1 {
        return client_error("streaming completions require exactly one prompt");
    }
    if stream && request.best_of.is_some() {
        return client_error("best_of is not supported with stream=true");
    }

    // `best_of` needs sampled-token probabilities for ranking even when the public response
    // deliberately omits logprobs.
    let public_logprobs = request.logprobs.is_some();
    if request.best_of.is_some() && !public_logprobs {
        sampling_params.logprobs = Some(0);
    }
    let units = prompts.len() * best_of as usize;
    let request_id = server_request_id("cmpl");
    let admission_request = AdmissionRequest {
        model: request.model.clone(),
        caller: CallerIdentity::current(),
        units: units as u32,
        received_at: timing.started_at,
        track_execution: false,
    };
    let mut admission = match state.generation.admit(&admission_request).await {
        Ok(permit) => permit,
        Err(error) => return openai_error(error),
    };
    let mut requests = Vec::with_capacity(units);
    for prompt in prompts {
        for _ in 0..best_of {
            requests.push(GenerationRequest {
                admission: Some(admission.split_one()),
                model: request.model.clone(),
                request_id: if requests.is_empty() {
                    request_id.clone()
                } else {
                    server_request_id("cmpl")
                },
                prompt: prompt.clone(),
                sampling_params: sampling_params.clone(),
                decode_options: decode_options(request.stop.clone()),
                intermediate: stream,
                priority: request.priority,
                cache_salt: request.cache_salt.clone(),
                session_id: request.session_id.clone(),
                arrival_time: timing.arrival_time,
                started_at: timing.started_at,
                tool_call_parser: ParserSelection::None,
                reasoning_parser: ParserSelection::None,
            });
        }
    }
    let mut requests = requests.into_iter();
    let Some(first_request) = requests.next() else {
        return openai_error(GenerationError::InvalidRequest);
    };
    // First acceptance establishes response metadata and the batch grant before
    // parallel candidates can encounter an initial Busy response.
    let first = match state.generation.generate(first_request).await {
        Ok(generated) => generated,
        Err(error) => return openai_error(error),
    };
    let pending = requests
        .map(|request| {
            let generation = state.generation.clone();
            let caller = admission_request.caller.clone();
            // SSE can poll after the HTTP handler's task-local scope has ended.
            async move { with_caller(caller, generation.generate(request)).await }
                .in_current_span()
                .boxed()
        })
        .collect();
    let options = CompletionResponseOptions {
        n: request.n as usize,
        candidates_per_prompt: best_of as usize,
        echo: request.echo,
        echo_without_generation: request.echo && request.max_tokens == Some(0),
        expose_logprobs: public_logprobs,
        return_token_ids: request.return_token_ids,
        return_tokens_as_token_ids: request.return_tokens_as_token_ids,
        return_prompt_token_ids: request.return_prompt_token_ids,
    };
    if stream {
        text_stream_many(first, pending, state.stream_idle, include_usage, options)
    } else {
        text_collected_many(first, pending, state.stream_idle, options).await
    }
}

/// Lowers chat input, selects optional storage, and returns shared generation as OpenAI JSON or SSE.
async fn chat_completions(
    State(api): State<ApiState>,
    Extension(state): Extension<RequestSnapshot>,
    timing: Option<Extension<RequestTiming>>,
    request: Result<Json<ChatCompletionRequest>, JsonRejection>,
) -> Response {
    let timing = timing.map_or_else(RequestTiming::now, |Extension(timing)| timing);
    let Json(mut request) = match request {
        Ok(request) => request,
        Err(error) => return json_error(error),
    };
    let id = server_request_id("chatcmpl");
    let metadata = request.metadata.take().unwrap_or_else(|| json!({}));
    if !metadata.is_object() {
        return client_error("metadata must be a JSON object");
    }
    // Preserve submitted content parts independently of the shared chat representation.
    let original_messages = request
        .store
        .unwrap_or(false)
        .then(|| request.messages.clone());
    let stream = request.stream;
    let include_usage = request.stream_options.include_usage;
    let include_reasoning = request.include_reasoning.unwrap_or(false);
    let (model, chat) = match prepare_chat(request, id) {
        Ok(prepared) => prepared,
        Err(error) => return client_error(error),
    };
    let archive = match original_messages {
        Some(messages) => {
            let Some(store) = api.store else {
                return crate::api::store_error(crate::store::StoreError::Disabled);
            };
            Some(stored::ChatArchive::new(
                store,
                CallerIdentity::current().caller,
                messages,
                metadata.clone(),
            ))
        }
        None => None,
    };
    let generated = match state
        .generate_chat(model, chat, include_reasoning, timing)
        .await
    {
        Ok(generated) => generated,
        Err(error) => return openai_error(error),
    };
    if stream {
        chat_stream_with_options(
            generated,
            state.stream_idle,
            include_usage,
            metadata,
            archive,
        )
    } else {
        chat_collected(generated, state.stream_idle, metadata, archive).await
    }
}

async fn list_models(State(state): State<ApiState>) -> Json<Value> {
    Json(json!({
        "object": "list",
        "data": (state.models)().into_iter().map(model_card).collect::<Vec<_>>(),
    }))
}

async fn retrieve_model(
    State(state): State<ApiState>,
    AxumPath(model): AxumPath<String>,
) -> Response {
    if (state.models)().iter().any(|candidate| candidate == &model) {
        Json(model_card(model)).into_response()
    } else {
        openai_error(GenerationError::ModelNotFound)
    }
}

/// Tokenizes text or rendered chat under the shared request admission and timing budget.
async fn tokenize(
    Extension(state): Extension<RequestSnapshot>,
    timing: Option<Extension<RequestTiming>>,
    request: Result<Json<TokenizeRequest>, JsonRejection>,
) -> Response {
    let timing = timing.map_or_else(RequestTiming::now, |Extension(timing)| timing);
    let origin = AdmissionOrigin {
        received_at: timing.started_at,
    };
    let Json(request) = match request {
        Ok(request) => request,
        Err(error) => return json_error(error),
    };
    let result = match request {
        TokenizeRequest::Completion(request) => {
            let model = match resolve_model(&state, request.model) {
                Ok(model) => model,
                Err(error) => return openai_error(error),
            };
            state
                .generation
                .tokenize(
                    &model,
                    Prompt::Text(request.prompt),
                    request.add_special_tokens,
                    request.return_token_strs,
                    origin,
                )
                .await
        }
        TokenizeRequest::Chat(request) => {
            let model = match resolve_model(&state, request.model) {
                Ok(model) => model,
                Err(error) => return openai_error(error),
            };
            if request.add_generation_prompt && request.continue_final_message {
                return client_error(
                    "add_generation_prompt and continue_final_message cannot both be true",
                );
            }
            if let Err(error) = validate_multimodal_input(&request.messages) {
                return client_error(error);
            }
            let messages = match request
                .messages
                .iter()
                .map(openai_message)
                .collect::<Result<Vec<_>, _>>()
            {
                Ok(messages) => messages,
                Err(error) => return client_error(error),
            };
            let tools = match request
                .tools
                .iter()
                .map(openai_tool)
                .collect::<Result<Vec<_>, _>>()
            {
                Ok(tools) => tools,
                Err(error) => return client_error(error),
            };
            let tool_context = match ResolvedToolContext::new(&messages, tools, None, true) {
                Ok(tool_context) => tool_context,
                Err(error) => return client_error(error.to_string()),
            };
            let generation_prompt_mode = if request.continue_final_message {
                GenerationPromptMode::ContinueFinalAssistant
            } else if request.add_generation_prompt {
                GenerationPromptMode::StartNewAssistant
            } else {
                GenerationPromptMode::NoGenerationPrompt
            };
            let chat = ChatRequest {
                request_id: server_request_id("tokenize"),
                messages,
                sampling_params: SamplingParams {
                    max_tokens: Some(1),
                    ..Default::default()
                },
                chat_options: ChatOptions {
                    generation_prompt_mode,
                    ..Default::default()
                },
                tool_context,
                decode_options: TextDecodeOptions::default(),
                intermediate: false,
                priority: 0,
                documents: None,
                cache_salt: None,
                add_special_tokens: request.add_special_tokens,
                data_parallel_rank: None,
                session_id: None,
                lora_request: None,
            };
            if let Err(error) = chat.validate() {
                return client_error(error.to_string());
            }
            state
                .generation
                .tokenize_chat(&model, chat, request.return_token_strs, origin)
                .await
        }
    };
    match result {
        Ok(result) => Json(TokenizeResponse {
            count: result.token_ids.len(),
            max_model_len: result.max_model_len,
            tokens: result.token_ids,
            token_strs: result.token_strs,
        })
        .into_response(),
        Err(error) => openai_error(error),
    }
}

/// Decodes token IDs through CPU-only admission without starting backend generation.
async fn detokenize(
    Extension(state): Extension<RequestSnapshot>,
    timing: Option<Extension<RequestTiming>>,
    request: Result<Json<DetokenizeRequest>, JsonRejection>,
) -> Response {
    let timing = timing.map_or_else(RequestTiming::now, |Extension(timing)| timing);
    let origin = AdmissionOrigin {
        received_at: timing.started_at,
    };
    let Json(request) = match request {
        Ok(request) => request,
        Err(error) => return json_error(error),
    };
    let model = match resolve_model(&state, request.model) {
        Ok(model) => model,
        Err(error) => return openai_error(error),
    };
    match state
        .generation
        .detokenize(&model, &request.tokens, origin)
        .await
    {
        Ok(prompt) => Json(DetokenizeResponse { prompt }).into_response(),
        Err(error) => openai_error(error),
    }
}

fn model_card(id: String) -> Value {
    let root = id.clone();
    let created = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    json!({
        "id": id,
        "object": "model",
        "created": created,
        "owned_by": "vllm",
        "root": root,
        "parent": null,
        "max_model_len": null,
        "permission": [],
    })
}

fn resolve_model(
    state: &RequestSnapshot,
    requested: Option<String>,
) -> Result<String, GenerationError> {
    if let Some(model) = requested {
        return state
            .models
            .iter()
            .any(|candidate| candidate == &model)
            .then_some(model)
            .ok_or(GenerationError::ModelNotFound);
    }
    match state.models.as_ref() {
        [model] => Ok(model.clone()),
        _ => Err(GenerationError::InvalidRequest),
    }
}

/// Returns a request-validation diagnostic in the OpenAI error envelope.
pub(super) fn client_error(message: impl Into<String>) -> Response {
    (
        StatusCode::BAD_REQUEST,
        Json(json!({"error": {
            "message": message.into(), "type": "invalid_request_error", "code": "invalid_request"
        }})),
    )
        .into_response()
}

/// Preserves extractor diagnostics and transport statuses; JSON content errors remain HTTP 400.
pub(super) fn json_error(error: JsonRejection) -> Response {
    let status = match error.status() {
        StatusCode::UNPROCESSABLE_ENTITY => StatusCode::BAD_REQUEST,
        status => status,
    };
    let mut response = client_error(error.body_text());
    *response.status_mut() = status;
    response
}

/// Maps every typed pre-response generation failure to a fixed OpenAI error.
pub(crate) fn openai_error(error: GenerationError) -> Response {
    let (status, message, kind, code) = match error {
        GenerationError::InvalidRequest => return client_error("invalid request"),
        GenerationError::ModelNotFound => (
            StatusCode::NOT_FOUND,
            "model not found",
            "invalid_request_error",
            "model_not_found",
        ),
        GenerationError::Unavailable | GenerationError::BackendBusy => (
            StatusCode::SERVICE_UNAVAILABLE,
            "generation service is unavailable",
            "server_error",
            "unavailable",
        ),
        GenerationError::Overloaded => (
            StatusCode::SERVICE_UNAVAILABLE,
            "generation service is overloaded",
            "server_error",
            "overloaded",
        ),
        GenerationError::QueueTimeout => (
            StatusCode::SERVICE_UNAVAILABLE,
            "admission queue timeout exceeded",
            "server_error",
            "queue_timeout",
        ),
        GenerationError::AdmissionCapacityExceeded => (
            StatusCode::BAD_REQUEST,
            "request fan-out exceeds configured admission capacity",
            "invalid_request_error",
            "concurrency_limit_exceeded",
        ),
        GenerationError::DeadlineExceeded => (
            StatusCode::GATEWAY_TIMEOUT,
            "request deadline exceeded",
            "server_error",
            "request_timeout",
        ),
        GenerationError::BackendRejected => (
            StatusCode::BAD_GATEWAY,
            "model server rejected the request",
            "server_error",
            "backend_rejected",
        ),
        GenerationError::BackendProtocol => (
            StatusCode::BAD_GATEWAY,
            "model server protocol failed",
            "server_error",
            "backend_protocol",
        ),
        GenerationError::RequestFailed => (
            StatusCode::BAD_GATEWAY,
            "model server request failed",
            "server_error",
            "request_failed",
        ),
        GenerationError::Internal => (
            StatusCode::INTERNAL_SERVER_ERROR,
            "internal server error",
            "server_error",
            "internal_error",
        ),
    };
    (
        status,
        Json(json!({"error": {"message": message, "type": kind, "code": code}})),
    )
        .into_response()
}
