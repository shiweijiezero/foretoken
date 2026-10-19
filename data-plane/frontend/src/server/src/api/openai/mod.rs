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
use foretoken_chat::{
    AssistantContentBlock, AssistantToolCall, ChatContent, ChatContentPart, ChatMessage,
    ChatOptions, ChatRequest, ChatTool, ChatToolChoice, GenerationPromptMode, ParserSelection,
    ResolvedToolContext,
};
use foretoken_engine_core_client::protocol::structured_outputs::StructuredOutputsParams;
use foretoken_text::{Prompt, SamplingParams, TextDecodeOptions};
use serde_json::{Value, json};

mod async_video;
mod output;
mod stored;
mod types;
mod video;

use self::output::{
    CompletionResponseOptions, chat_collected, chat_stream_with_options, text_collected_many,
    text_stream_many,
};
use self::types::{
    ChatCompletionRequest, CompletionRequest, DetokenizeRequest, DetokenizeResponse,
    OpenAiChatContent, OpenAiContentPart, OpenAiFunctionType, OpenAiMessage, OpenAiNamedToolChoice,
    OpenAiResponseFormat, OpenAiTool, OpenAiToolCall, OpenAiToolChoice, OpenAiToolChoiceMode, Stop,
    TokenizeRequest, TokenizeResponse,
};
use super::{ApiState, RequestTiming, server_request_id};
use crate::AdmissionOrigin;
use crate::runtime::{GenerationError, GenerationRequest, RequestSnapshot};
use foretoken_admission::{AdmissionRequest, CallerIdentity};

const MAX_COMPLETION_FAN_OUT: usize = 64;

type LoweredResponseFormat = (Option<Value>, Option<StructuredOutputsParams>);

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
        .route("/v1/videos/sync", post(video::generate))
        .merge(async_video::router())
        .merge(stored::router())
}

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
    let prompts = request.prompt.into_prompts();
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
    let mut generated = Vec::with_capacity(units);
    for prompt in prompts {
        for _ in 0..best_of {
            match state
                .generation
                .generate(GenerationRequest {
                    admission: Some(admission.split_one()),
                    model: request.model.clone(),
                    request_id: if generated.is_empty() {
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
                })
                .await
            {
                Ok(item) => generated.push(item),
                Err(error) => return openai_error(error),
            }
        }
    }
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
        text_stream_many(generated, state.stream_idle, include_usage, options)
    } else {
        text_collected_many(generated, state.stream_idle, options).await
    }
}

async fn chat_completions(
    State(api): State<ApiState>,
    Extension(state): Extension<RequestSnapshot>,
    timing: Option<Extension<RequestTiming>>,
    request: Result<Json<ChatCompletionRequest>, JsonRejection>,
) -> Response {
    let timing = timing.map_or_else(RequestTiming::now, |Extension(timing)| timing);
    let Json(request) = match request {
        Ok(request) => request,
        Err(error) => return json_error(error),
    };
    let id = server_request_id("chatcmpl");
    let metadata = request.metadata.unwrap_or_else(|| json!({}));
    if !metadata.is_object() {
        return client_error("metadata must be a JSON object");
    }
    // Preserve submitted content parts independently of the shared chat representation.
    let original_messages = request
        .store
        .unwrap_or(false)
        .then(|| request.messages.clone());
    let public_messages = match request
        .messages
        .into_iter()
        .map(serde_json::from_value::<OpenAiMessage>)
        .collect::<Result<Vec<_>, _>>()
    {
        Ok(messages) => messages,
        Err(error) => return client_error(format!("messages: {error}")),
    };
    if let Err(error) = validate_multimodal_input(&public_messages) {
        return client_error(error);
    }
    let messages = match public_messages
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
    let tool_choice = match request
        .tool_choice
        .as_ref()
        .map(openai_tool_choice)
        .transpose()
    {
        Ok(Some(choice)) => choice,
        // OpenAI's omitted tool_choice defaults to auto when tools are supplied.
        Ok(None) if !tools.is_empty() => ChatToolChoice::Auto,
        Ok(None) => ChatToolChoice::None,
        Err(error) => return client_error(error),
    };

    // Lower sampling and rendering options before entering the shared generation lifecycle.
    let stream = request.stream;
    let include_usage = request.stream_options.include_usage;
    if include_usage && !stream {
        return client_error("stream_options.include_usage requires stream=true");
    }
    if request.top_logprobs.is_some_and(|value| value > 20) {
        return client_error("top_logprobs must be at most 20");
    }
    if !request.logprobs && request.top_logprobs.is_some() {
        return client_error("top_logprobs requires logprobs=true");
    }
    let mut sampling_params = sampling(request.temperature, request.top_p, request.max_tokens);
    sampling_params.top_k = request.top_k;
    sampling_params.min_p = request.min_p;
    sampling_params.seed = request.seed;
    sampling_params.min_tokens = request.min_tokens;
    sampling_params.frequency_penalty = request.frequency_penalty;
    sampling_params.presence_penalty = request.presence_penalty;
    sampling_params.repetition_penalty = request.repetition_penalty;
    sampling_params.stop_token_ids = request.stop_token_ids.clone();
    sampling_params.ignore_eos = request.ignore_eos;
    sampling_params.logit_bias = request.logit_bias.clone();
    sampling_params.allowed_token_ids = request.allowed_token_ids.clone();
    sampling_params.bad_words = request.bad_words.clone();
    sampling_params.logprobs = request
        .logprobs
        .then_some(request.top_logprobs.unwrap_or_default() as i32);
    let (response_format, structured_output) = match response_format(request.response_format) {
        Ok(format) => format,
        Err(error) => return client_error(error),
    };
    if let Some(constraint) = structured_output {
        sampling_params.structured_outputs = Some(constraint);
    }
    let include_reasoning = request.include_reasoning.unwrap_or(false);
    let tool_context = match ResolvedToolContext::new(
        &messages,
        tools,
        Some(tool_choice),
        request.parallel_tool_calls,
    ) {
        Ok(tool_context) => tool_context,
        Err(error) => return client_error(error.to_string()),
    };
    let chat = ChatRequest {
        request_id: id.clone(),
        messages,
        sampling_params,
        chat_options: ChatOptions {
            reasoning_effort: request.reasoning_effort,
            response_format,
            ..Default::default()
        },
        tool_context,
        decode_options: decode_options(request.stop),
        intermediate: request.stream,
        priority: request.priority,
        documents: None,
        cache_salt: request.cache_salt.clone(),
        add_special_tokens: false,
        data_parallel_rank: None,
        session_id: request.session_id.clone(),
        lora_request: None,
    };
    if let Err(error) = chat.validate() {
        return client_error(error.to_string());
    }
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
        .generate_chat(request.model, chat, include_reasoning, timing)
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

fn sampling(
    temperature: Option<f32>,
    top_p: Option<f32>,
    max_tokens: Option<u32>,
) -> SamplingParams {
    SamplingParams {
        temperature,
        top_p,
        max_tokens,
        ..Default::default()
    }
}
fn completion_sampling(request: &CompletionRequest) -> SamplingParams {
    SamplingParams {
        temperature: request.temperature,
        top_p: request.top_p,
        top_k: request.top_k,
        seed: request.seed,
        max_tokens: if request.echo && request.max_tokens == Some(0) {
            Some(1)
        } else {
            request.max_tokens
        },
        min_tokens: request.min_tokens,
        logprobs: request.logprobs,
        prompt_logprobs: request
            .prompt_logprobs
            .or(request.logprobs.filter(|_| request.echo)),
        min_p: request.min_p,
        frequency_penalty: request.frequency_penalty,
        presence_penalty: request.presence_penalty,
        repetition_penalty: request.repetition_penalty,
        stop_token_ids: request.stop_token_ids.clone(),
        ignore_eos: request.ignore_eos,
        logit_bias: request.logit_bias.clone(),
        allowed_token_ids: request.allowed_token_ids.clone(),
        bad_words: request.bad_words.clone(),
        ..Default::default()
    }
}
fn decode_options(stop: Option<Stop>) -> TextDecodeOptions {
    TextDecodeOptions {
        stop_strings: stop.map(Stop::into_vec),
        ..Default::default()
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
/// Checks the supported image URL form without echoing media data.
fn validate_image_data_url(value: &str) -> Result<(), &'static str> {
    if value.split_once(',').is_some_and(|(metadata, payload)| {
        metadata.starts_with("data:image/") && metadata.ends_with(";base64") && !payload.is_empty()
    }) {
        Ok(())
    } else {
        Err("image_url.url must be a non-empty base64 image data URL")
    }
}

/// Applies the same image URL requirements to chat generation and token counting.
fn validate_multimodal_input(messages: &[OpenAiMessage]) -> Result<(), &'static str> {
    for part in messages
        .iter()
        .filter_map(|message| match &message.content {
            Some(OpenAiChatContent::Parts(parts)) => Some(parts.as_slice()),
            Some(OpenAiChatContent::Text(_)) | None => None,
        })
        .flatten()
    {
        if let OpenAiContentPart::ImageUrl { image_url } = part {
            validate_image_data_url(&image_url.url)?;
        }
    }
    Ok(())
}

/// Returns the renderer format and grammar constraint to lower.
fn response_format(
    format: Option<OpenAiResponseFormat>,
) -> Result<LoweredResponseFormat, &'static str> {
    match format {
        None => Ok((None, None)),
        Some(OpenAiResponseFormat::Text) => Ok((Some(json!({"type":"text"})), None)),
        Some(OpenAiResponseFormat::JsonObject) => Ok((
            Some(json!({"type":"json_object"})),
            Some(StructuredOutputsParams::json_object()),
        )),
        Some(OpenAiResponseFormat::JsonSchema { json_schema }) => {
            // Preserve OpenAI's named schema envelope when lowering the grammar.
            if json_schema.name.is_empty() {
                return Err("response_format.json_schema.name must not be empty");
            }
            if !json_schema.schema.is_object() {
                return Err("response_format.json_schema.schema must be a JSON object");
            }
            let mut envelope = serde_json::Map::from_iter([
                ("name".into(), Value::String(json_schema.name)),
                ("schema".into(), json_schema.schema.clone()),
            ]);
            if let Some(description) = json_schema.description {
                envelope.insert("description".into(), Value::String(description));
            }
            if let Some(strict) = json_schema.strict {
                envelope.insert("strict".into(), Value::Bool(strict));
            }
            let value = Value::Object(envelope);
            Ok((
                Some(json!({"type":"json_schema", "json_schema": value})),
                Some(StructuredOutputsParams::json(json_schema.schema)),
            ))
        }
    }
}

/// Validates and lowers an OpenAI function tool.
fn openai_tool(tool: &OpenAiTool) -> Result<ChatTool, &'static str> {
    let OpenAiTool {
        kind: OpenAiFunctionType::Function,
        function,
    } = tool;
    if function.name.is_empty() {
        return Err("tools[].function.name must not be empty");
    }
    if !function.parameters.is_object() {
        return Err("tools[].function.parameters must be a JSON object");
    }
    Ok(ChatTool {
        name: function.name.clone(),
        description: function.description.clone(),
        parameters: function.parameters.clone(),
        strict: function.strict,
    })
}

/// Resolves an OpenAI tool selection, rejecting empty function names.
fn openai_tool_choice(choice: &OpenAiToolChoice) -> Result<ChatToolChoice, &'static str> {
    Ok(match choice {
        OpenAiToolChoice::Mode(OpenAiToolChoiceMode::None) => ChatToolChoice::None,
        OpenAiToolChoice::Mode(OpenAiToolChoiceMode::Auto) => ChatToolChoice::Auto,
        OpenAiToolChoice::Mode(OpenAiToolChoiceMode::Required) => ChatToolChoice::Required,
        OpenAiToolChoice::Function(OpenAiNamedToolChoice {
            kind: OpenAiFunctionType::Function,
            function,
        }) if !function.name.is_empty() => ChatToolChoice::Function {
            name: function.name.clone(),
        },
        OpenAiToolChoice::Function(_) => return Err("tool_choice.function.name must not be empty"),
    })
}

/// Converts public content parts while retaining their deserialization diagnostics.
fn openai_content(content: &OpenAiChatContent) -> Result<ChatContent, String> {
    match content {
        OpenAiChatContent::Text(text) => Ok(ChatContent::Text(text.clone())),
        OpenAiChatContent::Parts(parts) => parts
            .iter()
            .map(|part| match part {
                OpenAiContentPart::Text { text } => Ok(ChatContentPart::text(text.clone())),
                OpenAiContentPart::ImageUrl { image_url } => serde_json::from_value(json!({
                    "type": "image_url",
                    "image_url": image_url.url.clone(),
                    "detail": image_url.detail.clone(),
                }))
                .map_err(|error| format!("messages[].content: {error}")),
            })
            .collect::<Result<Vec<_>, _>>()
            .map(ChatContent::Parts),
    }
}

/// Enforces role-specific fields before constructing the shared chat representation.
fn openai_message(message: &OpenAiMessage) -> Result<ChatMessage, String> {
    let content = message.content.as_ref().map(openai_content).transpose()?;
    Ok(match message.role.as_str() {
        "system" if message.tool_calls.is_empty() && message.tool_call_id.is_none() => {
            ChatMessage::System {
                content: content.ok_or("messages[].content is required for this role")?,
            }
        }
        "developer" if message.tool_calls.is_empty() && message.tool_call_id.is_none() => {
            ChatMessage::Developer {
                content: content.ok_or("messages[].content is required for this role")?,
                tools: None,
            }
        }
        "user" if message.tool_calls.is_empty() && message.tool_call_id.is_none() => {
            ChatMessage::User {
                content: content.ok_or("messages[].content is required for this role")?,
            }
        }
        "assistant" => {
            if message.tool_call_id.is_some() {
                return Err("assistant messages must not include tool_call_id".into());
            }
            let mut blocks = content
                .map(|content| {
                    content
                        .try_flatten_to_text()
                        .map(|text| AssistantContentBlock::Text { text })
                })
                .transpose()
                .map_err(|error| format!("assistant content: {error}"))?
                .into_iter()
                .collect::<Vec<_>>();
            blocks.extend(message.tool_calls.iter().map(|call| match call {
                OpenAiToolCall {
                    id,
                    kind: OpenAiFunctionType::Function,
                    function,
                } => AssistantContentBlock::ToolCall(AssistantToolCall {
                    id: id.clone(),
                    name: function.name.clone(),
                    arguments: function.arguments.clone(),
                }),
            }));
            if blocks.is_empty() {
                return Err("assistant messages must include content or tool_calls".into());
            }
            ChatMessage::assistant_blocks(blocks)
        }
        "tool" if message.tool_calls.is_empty() => ChatMessage::tool_response(
            content.ok_or("messages[].content is required for this role")?,
            message
                .tool_call_id
                .clone()
                .ok_or("tool messages require tool_call_id")?,
        ),
        "system" | "developer" | "user" => {
            return Err(
                "system, developer, and user messages must not include tool_calls or tool_call_id"
                    .into(),
            );
        }
        "tool" => return Err("tool messages must not include tool_calls".into()),
        _ => {
            return Err(
                "messages[].role must be system, developer, user, assistant, or tool".into(),
            );
        }
    })
}
