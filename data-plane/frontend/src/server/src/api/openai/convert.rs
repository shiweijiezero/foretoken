// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Lowers OpenAI inputs into the shared chat, sampling, and decoding contracts.

use foretoken_chat::{
    AssistantContentBlock, AssistantToolCall, ChatContent, ChatContentPart, ChatMessage,
    ChatOptions, ChatRequest, ChatTool, ChatToolChoice, ResolvedToolContext,
};
use foretoken_engine_core_client::protocol::structured_outputs::StructuredOutputsParams;
use foretoken_text::{SamplingParams, TextDecodeOptions};
use serde_json::{Value, json};

use super::types::{
    ChatCompletionRequest, CompletionRequest, OpenAiChatContent, OpenAiContentPart,
    OpenAiFunctionType, OpenAiMessage, OpenAiNamedToolChoice, OpenAiResponseFormat, OpenAiTool,
    OpenAiToolCall, OpenAiToolChoice, OpenAiToolChoiceMode, Stop,
};

type LoweredResponseFormat = (Option<Value>, Option<StructuredOutputsParams>);

/// Validates chat input for the HTTP handler and returns its model and shared generation request.
/// Storage and public response delivery remain with the handler.
pub(super) fn prepare_chat(
    request: ChatCompletionRequest,
    request_id: String,
) -> Result<(String, ChatRequest), String> {
    let public_messages = request
        .messages
        .into_iter()
        .map(serde_json::from_value::<OpenAiMessage>)
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("messages: {error}"))?;
    validate_multimodal_input(&public_messages)?;
    let messages = public_messages
        .iter()
        .map(openai_message)
        .collect::<Result<Vec<_>, _>>()?;
    let tools = request
        .tools
        .iter()
        .map(openai_tool)
        .collect::<Result<Vec<_>, _>>()?;
    let tool_choice = match request
        .tool_choice
        .as_ref()
        .map(openai_tool_choice)
        .transpose()?
    {
        Some(choice) => choice,
        // OpenAI's omitted tool_choice defaults to auto when tools are supplied.
        None if !tools.is_empty() => ChatToolChoice::Auto,
        None => ChatToolChoice::None,
    };

    // Lower sampling and rendering options before entering the shared generation lifecycle.
    let stream = request.stream;
    let include_usage = request.stream_options.include_usage;
    if include_usage && !stream {
        return Err("stream_options.include_usage requires stream=true".into());
    }
    if request.top_logprobs.is_some_and(|value| value > 20) {
        return Err("top_logprobs must be at most 20".into());
    }
    if !request.logprobs && request.top_logprobs.is_some() {
        return Err("top_logprobs requires logprobs=true".into());
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
    let (response_format, structured_output) = response_format(request.response_format)?;
    if let Some(constraint) = structured_output {
        sampling_params.structured_outputs = Some(constraint);
    }
    let tool_context = ResolvedToolContext::new(
        &messages,
        tools,
        Some(tool_choice),
        request.parallel_tool_calls,
    )
    .map_err(|error| error.to_string())?;
    let chat = ChatRequest {
        request_id,
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
    chat.validate().map_err(|error| error.to_string())?;
    Ok((request.model, chat))
}

/// Lowers completion sampling, requesting prompt scores when echo needs them.
pub(super) fn completion_sampling(request: &CompletionRequest) -> SamplingParams {
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

/// Normalizes OpenAI stop strings for the shared generation and tokenization decoders.
pub(super) fn decode_options(stop: Option<Stop>) -> TextDecodeOptions {
    TextDecodeOptions {
        stop_strings: stop.map(Stop::into_vec),
        ..Default::default()
    }
}

/// Applies the same image URL requirements to chat generation and token counting.
pub(super) fn validate_multimodal_input(messages: &[OpenAiMessage]) -> Result<(), &'static str> {
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

/// Validates and lowers an OpenAI function tool.
pub(super) fn openai_tool(tool: &OpenAiTool) -> Result<ChatTool, &'static str> {
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

/// Enforces role-specific fields before constructing the shared chat representation.
pub(super) fn openai_message(message: &OpenAiMessage) -> Result<ChatMessage, String> {
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
