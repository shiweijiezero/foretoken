// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Defines OpenAI-compatible request data transfer objects (DTOs) and HTTP handlers.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use axum::extract::DefaultBodyLimit;
use axum::extract::rejection::JsonRejection;
use axum::extract::{Path as AxumPath, State};
use axum::http::StatusCode;
use axum::middleware;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use foretoken_chat::{
    AssistantContentBlock, AssistantToolCall, ChatContent, ChatContentPart, ChatMessage,
    ChatOptions, ChatRequest, ChatTool, ChatToolChoice, GenerationPromptMode, ParserSelection,
    ReasoningEffort, ResolvedToolContext,
};
use foretoken_engine_core_client::protocol::structured_outputs::StructuredOutputsParams;
use foretoken_text::{Prompt, SamplingParams, TextDecodeOptions};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use uuid::Uuid;

use crate::response::{
    CompletionResponseOptions, chat_collected, chat_stream_with_options, text_collected_many,
    text_stream_many,
};
use crate::runtime::{Generation, GenerationError, GenerationRequest};

const MAX_HTTP_BODY_BYTES: usize = 48 * 1024 * 1024;
const MAX_COMPLETION_FAN_OUT: usize = 64;

type LoweredResponseFormat = (Option<Value>, Option<StructuredOutputsParams>);

#[derive(Clone)]
struct AppState {
    generation: Arc<dyn Generation>,
    models: Arc<dyn Fn() -> Vec<String> + Send + Sync>,
    stream_idle: Duration,
}

/// Creates the frontend HTTP router.
pub fn router(
    generation: Arc<dyn Generation>,
    models: Arc<dyn Fn() -> Vec<String> + Send + Sync>,
    stream_idle: Duration,
) -> Router {
    let public = Router::new()
        .route("/v1/models", get(list_models))
        .route("/v1/models/{model}", get(retrieve_model))
        .route("/tokenize", post(tokenize))
        .route("/detokenize", post(detokenize))
        .route("/v1/generate", post(completions))
        .route("/v1/completions", post(completions))
        .route("/v1/chat/completions", post(chat_completions));

    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/statusz", get(statusz))
        .route("/metrics", get(metrics))
        .route(
            "/internal/autoscaling/telemetry",
            get(autoscaling_telemetry),
        )
        .merge(public)
        .with_state(AppState {
            generation,
            models,
            stream_idle,
        })
        .layer(DefaultBodyLimit::max(MAX_HTTP_BODY_BYTES))
        .layer(middleware::from_fn(foretoken_metrics::track_http_metrics))
}

async fn healthz() -> StatusCode {
    StatusCode::OK
}
async fn readyz(State(state): State<AppState>) -> StatusCode {
    if state.generation.ready() {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}
async fn statusz(State(state): State<AppState>) -> Json<crate::runtime::RuntimeDiagnostics> {
    Json(state.generation.diagnostics())
}
async fn autoscaling_telemetry() -> Json<foretoken_metrics::AutoscalingTelemetry> {
    Json(foretoken_metrics::autoscaling_telemetry())
}

async fn metrics(State(state): State<AppState>) -> Response {
    let diagnostics = state.generation.diagnostics();
    foretoken_metrics::scrape_with_kv_index(
        &diagnostics.kv_index.state,
        diagnostics.kv_index.reason.as_deref(),
        diagnostics.kv_index.sources_healthy,
        diagnostics.kv_index.sources_total,
    )
    .await
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

async fn list_models(State(state): State<AppState>) -> Json<Value> {
    Json(json!({
        "object": "list",
        "data": (state.models)().into_iter().map(model_card).collect::<Vec<_>>(),
    }))
}

async fn retrieve_model(
    State(state): State<AppState>,
    AxumPath(model): AxumPath<String>,
) -> Response {
    if (state.models)().iter().any(|candidate| candidate == &model) {
        Json(model_card(model)).into_response()
    } else {
        openai_error(GenerationError::ModelNotFound)
    }
}

fn resolve_model(state: &AppState, requested: Option<String>) -> Result<String, GenerationError> {
    if let Some(model) = requested {
        return (state.models)()
            .iter()
            .any(|candidate| candidate == &model)
            .then_some(model)
            .ok_or(GenerationError::ModelNotFound);
    }
    let models = (state.models)();
    match models.as_slice() {
        [model] => Ok(model.clone()),
        _ => Err(GenerationError::InvalidRequest),
    }
}

async fn tokenize(
    State(state): State<AppState>,
    request: Result<Json<TokenizeRequest>, JsonRejection>,
) -> Response {
    let Json(request) = match request {
        Ok(request) => request,
        Err(_) => return client_error(),
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
                )
                .await
        }
        TokenizeRequest::Chat(request) => {
            let model = match resolve_model(&state, request.model) {
                Ok(model) => model,
                Err(error) => return openai_error(error),
            };
            if request.add_generation_prompt && request.continue_final_message {
                return client_error();
            }
            if validate_multimodal_input(&request.messages).is_err() {
                return client_error();
            }
            let messages = match request
                .messages
                .iter()
                .map(openai_message)
                .collect::<Result<Vec<_>, _>>()
            {
                Ok(messages) => messages,
                Err(error) => return openai_error(error),
            };
            let tools = match request
                .tools
                .iter()
                .map(openai_tool)
                .collect::<Result<Vec<_>, _>>()
            {
                Ok(tools) => tools,
                Err(error) => return openai_error(error),
            };
            let tool_context = match ResolvedToolContext::new(&messages, tools, None, true) {
                Ok(tool_context) => tool_context,
                Err(_) => return client_error(),
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
            state
                .generation
                .tokenize_chat(&model, chat, request.return_token_strs)
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
    State(state): State<AppState>,
    request: Result<Json<DetokenizeRequest>, JsonRejection>,
) -> Response {
    let Json(request) = match request {
        Ok(request) => request,
        Err(_) => return client_error(),
    };
    let model = match resolve_model(&state, request.model) {
        Ok(model) => model,
        Err(error) => return openai_error(error),
    };
    match state.generation.detokenize(&model, &request.tokens).await {
        Ok(prompt) => Json(DetokenizeResponse { prompt }).into_response(),
        Err(error) => openai_error(error),
    }
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum TokenizeRequest {
    Completion(TokenizeCompletionRequest),
    Chat(TokenizeChatRequest),
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TokenizeCompletionRequest {
    #[serde(default)]
    model: Option<String>,
    prompt: String,
    #[serde(default = "default_true")]
    add_special_tokens: bool,
    #[serde(default)]
    return_token_strs: bool,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct TokenizeChatRequest {
    #[serde(default)]
    model: Option<String>,
    messages: Vec<OpenAiMessage>,
    #[serde(default = "default_true")]
    add_generation_prompt: bool,
    #[serde(default)]
    continue_final_message: bool,
    #[serde(default)]
    add_special_tokens: bool,
    #[serde(default)]
    return_token_strs: bool,
    #[serde(default)]
    tools: Vec<OpenAiTool>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct DetokenizeRequest {
    #[serde(default)]
    model: Option<String>,
    tokens: Vec<u32>,
}

#[derive(Serialize)]
struct TokenizeResponse {
    count: usize,
    max_model_len: u32,
    tokens: Vec<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    token_strs: Option<Vec<String>>,
}

#[derive(Serialize)]
struct DetokenizeResponse {
    prompt: String,
}

fn default_true() -> bool {
    true
}

fn one() -> u32 {
    1
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum CompletionPrompt {
    Text(String),
    TokenIds(Vec<u32>),
    Texts(Vec<String>),
    TokenIdBatches(Vec<Vec<u32>>),
}

impl CompletionPrompt {
    fn into_prompts(self) -> Vec<Prompt> {
        match self {
            Self::Text(text) => vec![Prompt::Text(text)],
            Self::TokenIds(token_ids) => vec![Prompt::TokenIds(token_ids)],
            Self::Texts(texts) => texts.into_iter().map(Prompt::Text).collect(),
            Self::TokenIdBatches(batches) => batches.into_iter().map(Prompt::TokenIds).collect(),
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct CompletionRequest {
    model: String,
    prompt: CompletionPrompt,
    #[serde(default)]
    stream: bool,
    #[serde(default)]
    stream_options: OpenAiStreamOptions,
    #[serde(default = "one")]
    n: u32,
    #[serde(default)]
    best_of: Option<u32>,
    #[serde(default)]
    echo: bool,
    #[serde(default)]
    return_token_ids: bool,
    #[serde(default)]
    return_prompt_token_ids: bool,
    #[serde(default, rename = "request_id")]
    _client_request_id: Option<String>,
    #[serde(default)]
    temperature: Option<f32>,
    #[serde(default)]
    top_p: Option<f32>,
    #[serde(default)]
    top_k: Option<u32>,
    #[serde(default)]
    min_p: Option<f32>,
    #[serde(default)]
    seed: Option<i64>,
    #[serde(default)]
    max_tokens: Option<u32>,
    #[serde(default)]
    min_tokens: Option<u32>,
    #[serde(default)]
    frequency_penalty: Option<f32>,
    #[serde(default)]
    presence_penalty: Option<f32>,
    #[serde(default)]
    repetition_penalty: Option<f32>,
    #[serde(default)]
    stop_token_ids: Option<Vec<u32>>,
    #[serde(default)]
    ignore_eos: bool,
    #[serde(default)]
    logit_bias: Option<HashMap<u32, f32>>,
    #[serde(default)]
    allowed_token_ids: Option<Vec<u32>>,
    #[serde(default)]
    bad_words: Option<Vec<String>>,
    #[serde(default)]
    logprobs: Option<i32>,
    #[serde(default)]
    prompt_logprobs: Option<i32>,
    #[serde(default)]
    priority: i32,
    #[serde(default)]
    cache_salt: Option<String>,
    #[serde(default)]
    session_id: Option<String>,
    #[serde(default)]
    stop: Option<Stop>,
}

#[derive(Debug, Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiStreamOptions {
    #[serde(default)]
    include_usage: bool,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(untagged)]
enum Stop {
    One(String),
    Many(Vec<String>),
}
impl Stop {
    fn into_vec(self) -> Vec<String> {
        match self {
            Self::One(s) => vec![s],
            Self::Many(v) => v,
        }
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ChatCompletionRequest {
    model: String,
    messages: Vec<OpenAiMessage>,
    #[serde(default)]
    stream: bool,
    #[serde(default)]
    stream_options: OpenAiStreamOptions,
    #[serde(default, rename = "request_id")]
    _client_request_id: Option<String>,
    #[serde(default)]
    temperature: Option<f32>,
    #[serde(default)]
    top_p: Option<f32>,
    #[serde(default)]
    top_k: Option<u32>,
    #[serde(default)]
    min_p: Option<f32>,
    #[serde(default)]
    seed: Option<i64>,
    #[serde(default)]
    max_tokens: Option<u32>,
    #[serde(default)]
    min_tokens: Option<u32>,
    #[serde(default)]
    frequency_penalty: Option<f32>,
    #[serde(default)]
    presence_penalty: Option<f32>,
    #[serde(default)]
    repetition_penalty: Option<f32>,
    #[serde(default)]
    stop_token_ids: Option<Vec<u32>>,
    #[serde(default)]
    ignore_eos: bool,
    #[serde(default)]
    logit_bias: Option<HashMap<u32, f32>>,
    #[serde(default)]
    allowed_token_ids: Option<Vec<u32>>,
    #[serde(default)]
    bad_words: Option<Vec<String>>,
    #[serde(default)]
    logprobs: bool,
    #[serde(default)]
    top_logprobs: Option<u32>,
    #[serde(default)]
    priority: i32,
    #[serde(default)]
    cache_salt: Option<String>,
    #[serde(default)]
    session_id: Option<String>,
    #[serde(default)]
    stop: Option<Stop>,
    #[serde(default)]
    tools: Vec<OpenAiTool>,
    #[serde(default)]
    tool_choice: Option<OpenAiToolChoice>,
    #[serde(default)]
    parallel_tool_calls: bool,
    #[serde(default)]
    response_format: Option<OpenAiResponseFormat>,
    #[serde(default)]
    reasoning_effort: Option<ReasoningEffort>,
    #[serde(default)]
    include_reasoning: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiMessage {
    role: String,
    #[serde(default)]
    content: Option<OpenAiChatContent>,
    #[serde(default)]
    tool_calls: Vec<OpenAiToolCall>,
    #[serde(default)]
    tool_call_id: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum OpenAiChatContent {
    Text(String),
    Parts(Vec<OpenAiContentPart>),
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
enum OpenAiContentPart {
    Text { text: String },
    ImageUrl { image_url: OpenAiImageUrl },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiImageUrl {
    url: String,
    #[serde(default)]
    detail: Option<Value>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiToolCall {
    id: String,
    #[serde(rename = "type")]
    kind: OpenAiFunctionType,
    function: OpenAiFunctionCall,
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum OpenAiFunctionType {
    Function,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiFunctionCall {
    name: String,
    arguments: String,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiTool {
    #[serde(rename = "type")]
    kind: OpenAiFunctionType,
    function: OpenAiToolFunction,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiToolFunction {
    name: String,
    #[serde(default)]
    description: Option<String>,
    parameters: Value,
    #[serde(default)]
    strict: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(untagged)]
enum OpenAiToolChoice {
    Mode(OpenAiToolChoiceMode),
    Function(OpenAiNamedToolChoice),
}

#[derive(Debug, Deserialize)]
#[serde(rename_all = "snake_case")]
enum OpenAiToolChoiceMode {
    None,
    Auto,
    Required,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiNamedToolChoice {
    #[serde(rename = "type")]
    kind: OpenAiFunctionType,
    function: OpenAiNamedFunction,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiNamedFunction {
    name: String,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum OpenAiResponseFormat {
    Text,
    JsonObject,
    JsonSchema { json_schema: OpenAiJsonSchema },
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct OpenAiJsonSchema {
    name: String,
    schema: Value,
    #[serde(default)]
    description: Option<String>,
    #[serde(default)]
    strict: Option<bool>,
}

/// Generates the backend and Mooncake request identity for one HTTP generation.
fn server_request_id(prefix: &str) -> String {
    format!("{prefix}-{}", Uuid::new_v4())
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
        max_tokens: request.max_tokens,
        min_tokens: request.min_tokens,
        logprobs: request.logprobs,
        prompt_logprobs: request.prompt_logprobs,
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
fn client_error() -> Response {
    openai_error(GenerationError::InvalidRequest)
}

/// Maps every typed pre-response generation failure to a fixed OpenAI error.
pub(crate) fn openai_error(error: GenerationError) -> Response {
    let (status, message, kind, code) = match error {
        GenerationError::InvalidRequest => (
            StatusCode::BAD_REQUEST,
            "invalid request",
            "invalid_request_error",
            "invalid_request",
        ),
        GenerationError::ModelNotFound => (
            StatusCode::NOT_FOUND,
            "model not found",
            "invalid_request_error",
            "model_not_found",
        ),
        GenerationError::Unavailable => (
            StatusCode::SERVICE_UNAVAILABLE,
            "generation service is unavailable",
            "server_error",
            "unavailable",
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
async fn completions(
    State(state): State<AppState>,
    request: Result<Json<CompletionRequest>, JsonRejection>,
) -> Response {
    let Json(request) = match request {
        Ok(request) => request,
        Err(_) => return client_error(),
    };
    let stream = request.stream;
    let include_usage = request.stream_options.include_usage;
    let mut sampling_params = completion_sampling(&request);
    let prompts = request.prompt.into_prompts();
    let best_of = request.best_of.unwrap_or(request.n);
    if include_usage && !stream
        || prompts.is_empty()
        || request.n == 0
        || best_of < request.n
        || prompts.len().saturating_mul(best_of as usize) > MAX_COMPLETION_FAN_OUT
        || (stream && (prompts.len() != 1 || request.best_of.is_some()))
    {
        return client_error();
    }

    // `best_of` needs sampled-token probabilities for ranking even when the public response
    // deliberately omits logprobs.
    let public_logprobs = request.logprobs.is_some();
    if request.best_of.is_some() && !public_logprobs {
        sampling_params.logprobs = Some(0);
    }
    let mut generated = Vec::with_capacity(prompts.len() * best_of as usize);
    for prompt in prompts {
        for _ in 0..best_of {
            match state
                .generation
                .generate(GenerationRequest {
                    model: request.model.clone(),
                    request_id: server_request_id("cmpl"),
                    prompt: prompt.clone(),
                    sampling_params: sampling_params.clone(),
                    decode_options: decode_options(request.stop.clone()),
                    intermediate: stream,
                    priority: request.priority,
                    cache_salt: request.cache_salt.clone(),
                    session_id: request.session_id.clone(),
                    arrival_time: Some(vllm_llm::current_unix_timestamp_secs()),
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
    if stream {
        text_stream_many(
            generated,
            state.stream_idle,
            include_usage,
            request.return_token_ids,
            request.return_prompt_token_ids,
        )
    } else {
        text_collected_many(
            generated,
            state.stream_idle,
            CompletionResponseOptions {
                n: request.n as usize,
                candidates_per_prompt: best_of as usize,
                echo: request.echo,
                expose_logprobs: public_logprobs,
                return_token_ids: request.return_token_ids,
                return_prompt_token_ids: request.return_prompt_token_ids,
            },
        )
        .await
    }
}

async fn chat_completions(
    State(state): State<AppState>,
    request: Result<Json<ChatCompletionRequest>, JsonRejection>,
) -> Response {
    let Json(request) = match request {
        Ok(request) => request,
        Err(_) => return client_error(),
    };
    let id = server_request_id("chatcmpl");
    if validate_multimodal_input(&request.messages).is_err() {
        return client_error();
    }
    let messages = match request
        .messages
        .iter()
        .map(openai_message)
        .collect::<Result<Vec<_>, _>>()
    {
        Ok(messages) => messages,
        Err(_) => return client_error(),
    };
    let tools = match request
        .tools
        .iter()
        .map(openai_tool)
        .collect::<Result<Vec<_>, _>>()
    {
        Ok(tools) => tools,
        Err(_) => return client_error(),
    };
    let mut tool_choice = match request
        .tool_choice
        .as_ref()
        .map(openai_tool_choice)
        .transpose()
    {
        Ok(Some(choice)) => choice,
        Ok(None) => ChatToolChoice::None,
        Err(_) => return client_error(),
    };
    if !tools.is_empty() && matches!(&tool_choice, ChatToolChoice::None) {
        // OpenAI's omitted tool_choice defaults to auto when tools are supplied.
        tool_choice = ChatToolChoice::Auto;
    }
    chat_with_request(state, request, id, messages, tools, tool_choice).await
}

async fn chat_with_request(
    state: AppState,
    request: ChatCompletionRequest,
    id: String,
    messages: Vec<ChatMessage>,
    tools: Vec<ChatTool>,
    tool_choice: ChatToolChoice,
) -> Response {
    let stream = request.stream;
    let include_usage = request.stream_options.include_usage;
    if include_usage && !stream {
        return client_error();
    }
    if request.top_logprobs.is_some_and(|value| value > 20)
        || (!request.logprobs && request.top_logprobs.is_some())
    {
        return client_error();
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
        Err(_) => return client_error(),
    };
    if let Some(constraint) = structured_output {
        sampling_params.structured_outputs = Some(constraint);
    }
    let include_reasoning = request.include_reasoning.unwrap_or(false);
    let reasoning_requested = include_reasoning
        || matches!(request.reasoning_effort, Some(effort) if effort != ReasoningEffort::None);
    let tool_requested = !tools.is_empty() && !matches!(&tool_choice, ChatToolChoice::None);
    let tool_context = match ResolvedToolContext::new(
        &messages,
        tools,
        Some(tool_choice),
        request.parallel_tool_calls,
    ) {
        Ok(tool_context) => tool_context,
        Err(_) => return client_error(),
    };
    let chat = ChatRequest {
        request_id: id.clone(),
        messages,
        sampling_params: sampling_params.clone(),
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
    if chat.validate().is_err() {
        return client_error();
    }
    let generated = match state
        .generation
        .generate_chat(
            GenerationRequest {
                model: request.model,
                request_id: id,
                prompt: Prompt::Text(String::new()),
                sampling_params,
                decode_options: chat.decode_options.clone(),
                intermediate: stream,
                priority: chat.priority,
                cache_salt: chat.cache_salt.clone(),
                session_id: chat.session_id.clone(),
                arrival_time: None,
                tool_call_parser: if tool_requested {
                    ParserSelection::Auto
                } else {
                    ParserSelection::None
                },
                reasoning_parser: if reasoning_requested {
                    ParserSelection::Auto
                } else {
                    ParserSelection::None
                },
            },
            chat,
            include_reasoning,
        )
        .await
    {
        Ok(generated) => generated,
        Err(error) => return openai_error(error),
    };
    if stream {
        chat_stream_with_options(generated, state.stream_idle, include_usage)
    } else {
        chat_collected(generated, state.stream_idle).await
    }
}

fn validate_image_data_url(value: &str) -> Result<(), GenerationError> {
    let (metadata, payload) = value
        .split_once(',')
        .ok_or(GenerationError::InvalidRequest)?;
    if !metadata.starts_with("data:image/") || !metadata.ends_with(";base64") || payload.is_empty()
    {
        return Err(GenerationError::InvalidRequest);
    }
    Ok(())
}

fn validate_multimodal_input(messages: &[OpenAiMessage]) -> Result<(), GenerationError> {
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
) -> Result<LoweredResponseFormat, GenerationError> {
    match format {
        None => Ok((None, None)),
        Some(OpenAiResponseFormat::Text) => Ok((Some(json!({"type":"text"})), None)),
        Some(OpenAiResponseFormat::JsonObject) => Ok((
            Some(json!({"type":"json_object"})),
            Some(StructuredOutputsParams::json_object()),
        )),
        Some(OpenAiResponseFormat::JsonSchema { json_schema }) => {
            // The the local vLLM build source type accepts a JSON value. Restrict the public shape to
            // OpenAI's named schema envelope and require an object before lowering it.
            if json_schema.name.is_empty() || !json_schema.schema.is_object() {
                return Err(GenerationError::InvalidRequest);
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

fn openai_tool(tool: &OpenAiTool) -> Result<ChatTool, GenerationError> {
    let OpenAiTool {
        kind: OpenAiFunctionType::Function,
        function,
    } = tool;
    if function.name.is_empty() || !function.parameters.is_object() {
        return Err(GenerationError::InvalidRequest);
    }
    Ok(ChatTool {
        name: function.name.clone(),
        description: function.description.clone(),
        parameters: function.parameters.clone(),
        strict: function.strict,
    })
}
fn openai_tool_choice(choice: &OpenAiToolChoice) -> Result<ChatToolChoice, GenerationError> {
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
        OpenAiToolChoice::Function(_) => return Err(GenerationError::InvalidRequest),
    })
}
fn openai_content(content: &OpenAiChatContent) -> Result<ChatContent, GenerationError> {
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
                .map_err(|_| GenerationError::InvalidRequest),
            })
            .collect::<Result<Vec<_>, _>>()
            .map(ChatContent::Parts),
    }
}

fn openai_message(message: &OpenAiMessage) -> Result<ChatMessage, GenerationError> {
    let content = message.content.as_ref().map(openai_content).transpose()?;
    Ok(match message.role.as_str() {
        "system" if message.tool_calls.is_empty() && message.tool_call_id.is_none() => {
            ChatMessage::System {
                content: content.ok_or(GenerationError::InvalidRequest)?,
            }
        }
        "developer" if message.tool_calls.is_empty() && message.tool_call_id.is_none() => {
            ChatMessage::Developer {
                content: content.ok_or(GenerationError::InvalidRequest)?,
                tools: None,
            }
        }
        "user" if message.tool_calls.is_empty() && message.tool_call_id.is_none() => {
            ChatMessage::User {
                content: content.ok_or(GenerationError::InvalidRequest)?,
            }
        }
        "assistant" => {
            if message.tool_call_id.is_some() {
                return Err(GenerationError::InvalidRequest);
            }
            let mut blocks = content
                .map(|content| {
                    content
                        .try_flatten_to_text()
                        .map(|text| AssistantContentBlock::Text { text })
                })
                .transpose()
                .map_err(|_| GenerationError::InvalidRequest)?
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
                return Err(GenerationError::InvalidRequest);
            }
            ChatMessage::assistant_blocks(blocks)
        }
        "tool" if message.tool_calls.is_empty() => ChatMessage::tool_response(
            content.ok_or(GenerationError::InvalidRequest)?,
            message
                .tool_call_id
                .clone()
                .ok_or(GenerationError::InvalidRequest)?,
        ),
        _ => return Err(GenerationError::InvalidRequest),
    })
}
