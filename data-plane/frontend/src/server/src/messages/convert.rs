// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request lowering adapted from vLLM PR #52896, commit
//! 1e19c0826853371a5549f23d83678b7e56b8baea, routes/anthropic/convert.rs.
//! Rendering and model-specific decisions remain in the shared chat processor.

use std::collections::HashMap;

use foretoken_chat::{
    AssistantContentBlock, AssistantToolCall, ChatContent, ChatContentPart, ChatMessage,
    ChatOptions, ChatRequest, ChatTool, ChatToolChoice, ResolvedToolContext,
};
use foretoken_engine_core_client::protocol::structured_outputs::StructuredOutputsParams;
use foretoken_text::{SamplingParams, TextDecodeOptions};
use serde_json::{Value, json};

use super::error::AnthropicApiError;
use super::types::*;

/// Prepares one generation request using the same lowering as token counting.
pub(super) fn prepare_messages_request(
    request: AnthropicMessagesRequest,
    request_id: String,
) -> Result<(String, ChatRequest, bool), AnthropicApiError> {
    if request.max_tokens == 0 {
        return Err(AnthropicApiError::invalid(
            "max_tokens must be greater than zero",
        ));
    }
    if request.kv_transfer_params.is_some() || request.ec_transfer_params.is_some() {
        return Err(AnthropicApiError::invalid(
            "Transfer parameters are managed by the service",
        ));
    }
    if request.container.is_some() || request.mcp_servers.is_some() {
        return Err(AnthropicApiError::invalid(
            "Server-managed containers and MCP tools are not supported",
        ));
    }
    validate_context_management(request.context_management.as_ref())?;
    let (chat_options, show_thinking) = options(
        request.thinking,
        request.output_config,
        request.chat_template_kwargs,
    )?;
    let mut sampling = SamplingParams {
        temperature: request.temperature,
        top_p: request.top_p,
        top_k: request.top_k,
        max_tokens: Some(request.max_tokens),
        ..Default::default()
    };
    if let Some(format) = &chat_options.response_format {
        sampling.structured_outputs = Some(StructuredOutputsParams::json(
            format["json_schema"]["schema"].clone(),
        ));
    }
    let chat = prepare_chat(
        request_id,
        request.system,
        request.messages,
        request.tools,
        request.tool_choice,
        sampling,
        chat_options,
        TextDecodeOptions {
            stop_strings: request.stop_sequences,
            ..Default::default()
        },
        request.stream,
        request.cache_salt,
    )?;
    if request.model.is_empty() {
        return Err(AnthropicApiError::invalid("model is required"));
    }
    Ok((request.model, chat, show_thinking))
}

/// Renders the same message/tool representation without dispatching inference.
pub(super) fn prepare_count_tokens_request(
    request: AnthropicCountTokensRequest,
    request_id: String,
) -> Result<(String, ChatRequest), AnthropicApiError> {
    if request.model.is_empty() {
        return Err(AnthropicApiError::invalid("model is required"));
    }
    let (chat_options, _) = options(
        request.thinking,
        request.output_config,
        request.chat_template_kwargs,
    )?;
    let chat = prepare_chat(
        request_id,
        request.system,
        request.messages,
        request.tools,
        request.tool_choice,
        SamplingParams {
            max_tokens: Some(1),
            ..Default::default()
        },
        chat_options,
        TextDecodeOptions::default(),
        false,
        None,
    )?;
    Ok((request.model, chat))
}

/// Builds the shared internal request after protocol-specific field validation.
#[allow(clippy::too_many_arguments)]
fn prepare_chat(
    request_id: String,
    system: Option<SystemPrompt>,
    messages: Vec<AnthropicMessage>,
    tools: Option<Vec<AnthropicTool>>,
    tool_choice: Option<AnthropicToolChoice>,
    sampling_params: SamplingParams,
    chat_options: ChatOptions,
    decode_options: TextDecodeOptions,
    intermediate: bool,
    cache_salt: Option<String>,
) -> Result<ChatRequest, AnthropicApiError> {
    if messages.is_empty() {
        return Err(AnthropicApiError::invalid("messages must not be empty"));
    }
    let parallel = parallel_tool_calls(tool_choice.as_ref());
    let tools = convert_tools(tools)?;
    let choice = convert_tool_choice(tool_choice.as_ref(), !tools.is_empty());
    let messages = convert_messages(system, messages)?;
    let tool_context = ResolvedToolContext::new(&messages, tools, choice, parallel)
        .map_err(|_| AnthropicApiError::invalid("Invalid tool declarations or tool_choice"))?;
    let chat = ChatRequest {
        request_id,
        messages,
        sampling_params,
        chat_options,
        tool_context,
        decode_options,
        intermediate,
        priority: 0,
        documents: None,
        cache_salt,
        add_special_tokens: false,
        data_parallel_rank: None,
        session_id: None,
        lora_request: None,
    };
    chat.validate()
        .map_err(|_| AnthropicApiError::invalid("Invalid conversation"))?;
    Ok(chat)
}

/// Converts the prompt and preserves message/block encounter order.
fn convert_messages(
    system: Option<SystemPrompt>,
    messages: Vec<AnthropicMessage>,
) -> Result<Vec<ChatMessage>, AnthropicApiError> {
    let mut out = Vec::new();
    let system_text = match system {
        Some(SystemPrompt::Text(text)) => text,
        Some(SystemPrompt::Blocks(blocks)) => blocks
            .into_iter()
            .map(|block| match block {
                SystemTextBlock::Text { text } => text,
            })
            .collect::<Vec<_>>()
            .join("\n"),
        None => String::new(),
    };
    if !system_text.is_empty() {
        out.push(ChatMessage::system(system_text));
    }
    for message in messages {
        match message.role {
            AnthropicRole::System => {
                let text = match message.content {
                    MessageContent::Text(text) => text,
                    MessageContent::Blocks(blocks) => {
                        let mut texts = Vec::new();
                        for block in blocks {
                            match block {
                                AnthropicContentBlock::Text { text } => texts.push(text),
                                _ => {
                                    return Err(AnthropicApiError::invalid(
                                        "System messages accept only text",
                                    ));
                                }
                            }
                        }
                        texts.join("\n")
                    }
                };
                // Consecutive system messages can be combined without moving instructions across turns.
                if let Some(ChatMessage::System {
                    content: ChatContent::Text(previous),
                }) = out.last_mut()
                {
                    previous.push('\n');
                    previous.push_str(&text);
                } else {
                    out.push(ChatMessage::system(text));
                }
            }
            AnthropicRole::User => convert_user_message(message.content, &mut out)?,
            AnthropicRole::Assistant => convert_assistant_message(message.content, &mut out)?,
        }
    }
    Ok(out)
}

/// Flushes user content around tool results without reordering the input history.
fn convert_user_message(
    content: MessageContent,
    out: &mut Vec<ChatMessage>,
) -> Result<(), AnthropicApiError> {
    let blocks = match content {
        MessageContent::Text(text) => {
            out.push(ChatMessage::user(text));
            return Ok(());
        }
        MessageContent::Blocks(blocks) => blocks,
    };
    let mut parts = Vec::new();
    for block in blocks {
        match block {
            AnthropicContentBlock::Text { text } => parts.push(ChatContentPart::text(text)),
            AnthropicContentBlock::Image { source } => parts.push(ChatContentPart::image_url(
                convert_image_source_to_url(source)?,
            )),
            AnthropicContentBlock::ToolResult {
                tool_use_id,
                content,
                is_error,
            } => {
                if !parts.is_empty() {
                    out.push(ChatMessage::user(ChatContent::Parts(std::mem::take(
                        &mut parts,
                    ))));
                }
                convert_user_tool_result(tool_use_id, content, is_error.unwrap_or(false), out)?;
            }
            _ => {
                return Err(AnthropicApiError::invalid(
                    "Unsupported content block in user message",
                ));
            }
        }
    }
    if !parts.is_empty() {
        out.push(ChatMessage::user(ChatContent::Parts(parts)));
    }
    Ok(())
}

/// Converts tool results to the shared tool-response message while retaining images.
fn convert_user_tool_result(
    id: String,
    content: Option<ToolResultContent>,
    is_error: bool,
    out: &mut Vec<ChatMessage>,
) -> Result<(), AnthropicApiError> {
    let mut parts = match content {
        None => Vec::new(),
        Some(ToolResultContent::Text(text)) => vec![ChatContentPart::text(text)],
        Some(ToolResultContent::Blocks(blocks)) => {
            let mut parts = Vec::new();
            for block in blocks {
                match block {
                    AnthropicContentBlock::Text { text } => parts.push(ChatContentPart::text(text)),
                    AnthropicContentBlock::Image { source } => parts.push(
                        ChatContentPart::image_url(convert_image_source_to_url(source)?),
                    ),
                    _ => return Err(AnthropicApiError::invalid("Unsupported tool result block")),
                }
            }
            parts
        }
    };
    if is_error {
        parts.insert(0, ChatContentPart::text("Tool execution failed:\n"));
    }
    out.push(ChatMessage::tool_response(ChatContent::Parts(parts), id));
    Ok(())
}

/// Preserves assistant thinking/text/tool-use interleaving from the upstream converter.
fn convert_assistant_message(
    content: MessageContent,
    out: &mut Vec<ChatMessage>,
) -> Result<(), AnthropicApiError> {
    let blocks = match content {
        MessageContent::Text(text) => {
            out.push(ChatMessage::assistant_text(text));
            return Ok(());
        }
        MessageContent::Blocks(blocks) => blocks,
    };
    let mut content = Vec::new();
    for block in blocks {
        match block {
            AnthropicContentBlock::Text { text } => {
                content.push(AssistantContentBlock::Text { text })
            }
            AnthropicContentBlock::Thinking {
                thinking,
                signature,
            } => {
                // Plaintext is client-provided history; an opaque signature is not authenticated here.
                if thinking.is_empty() && signature.as_ref().is_some_and(|s| !s.is_empty()) {
                    return Err(AnthropicApiError::invalid(
                        "Thinking history requires plaintext; opaque signatures cannot be decoded",
                    ));
                }
                content.push(AssistantContentBlock::Reasoning { text: thinking });
            }
            AnthropicContentBlock::ToolUse { id, name, input } => {
                if !input.is_object() {
                    return Err(AnthropicApiError::invalid(
                        "tool_use input must be an object",
                    ));
                }
                content.push(AssistantContentBlock::ToolCall(AssistantToolCall {
                    id,
                    name,
                    arguments: input.to_string(),
                }));
            }
            AnthropicContentBlock::RedactedThinking { data } => {
                let _ = data;
                return Err(AnthropicApiError::invalid(
                    "Redacted thinking replay is not supported",
                ));
            }
            AnthropicContentBlock::ToolReference { tool_name } => {
                let _ = tool_name;
                return Err(AnthropicApiError::invalid(
                    "Tool-reference blocks are not supported",
                ));
            }
            _ => {
                return Err(AnthropicApiError::invalid(
                    "Unsupported content block in assistant message",
                ));
            }
        }
    }
    if content.is_empty() {
        return Err(AnthropicApiError::invalid(
            "Assistant messages must contain content",
        ));
    }
    out.push(ChatMessage::assistant_blocks(content));
    Ok(())
}

/// Reuses the existing inline-image boundary instead of fetching arbitrary URLs.
fn convert_image_source_to_url(source: ImageSource) -> Result<String, AnthropicApiError> {
    match source {
        ImageSource::Base64 { media_type, data }
            if media_type.starts_with("image/") && !data.is_empty() =>
        {
            Ok(format!("data:{media_type};base64,{data}"))
        }
        ImageSource::Base64 { .. } => {
            Err(AnthropicApiError::invalid("Invalid base64 image source"))
        }
        ImageSource::Url { url } => {
            let _ = url;
            Err(AnthropicApiError::invalid(
                "Use an inline base64 image source",
            ))
        }
    }
}

/// Maps the upstream Anthropic choice variants without overwriting explicit none.
fn convert_tool_choice(
    choice: Option<&AnthropicToolChoice>,
    has_tools: bool,
) -> Option<ChatToolChoice> {
    match choice {
        Some(AnthropicToolChoice::Auto { .. }) => Some(ChatToolChoice::Auto),
        Some(AnthropicToolChoice::Any { .. }) => Some(ChatToolChoice::Required),
        Some(AnthropicToolChoice::None) => Some(ChatToolChoice::None),
        Some(AnthropicToolChoice::Tool { name, .. }) => {
            Some(ChatToolChoice::Function { name: name.clone() })
        }
        None if has_tools => Some(ChatToolChoice::Auto),
        None => None,
    }
}

/// Lowers supported client tools to the upstream shared tool representation.
fn convert_tools(tools: Option<Vec<AnthropicTool>>) -> Result<Vec<ChatTool>, AnthropicApiError> {
    tools
        .unwrap_or_default()
        .into_iter()
        .map(|tool| {
            if tool.tool_type.as_deref().is_some_and(|t| t != "custom") || tool.defer_loading {
                return Err(AnthropicApiError::invalid(
                    "Server tools and deferred tool loading are not supported",
                ));
            }
            if tool.name.is_empty() {
                return Err(AnthropicApiError::invalid("Tool name must not be empty"));
            }
            let Some(Value::Object(mut parameters)) = tool.input_schema else {
                return Err(AnthropicApiError::invalid(
                    "Tool input_schema must be an object",
                ));
            };
            parameters.entry("type").or_insert(json!("object"));
            Ok(ChatTool {
                name: tool.name,
                description: tool.description,
                parameters: Value::Object(parameters),
                strict: tool.strict,
            })
        })
        .collect()
}

/// Converts Anthropic parallel-tool-use control to the shared renderer/parser setting.
fn parallel_tool_calls(choice: Option<&AnthropicToolChoice>) -> bool {
    match choice {
        Some(
            AnthropicToolChoice::Auto {
                disable_parallel_tool_use,
            }
            | AnthropicToolChoice::Any {
                disable_parallel_tool_use,
            }
            | AnthropicToolChoice::Tool {
                disable_parallel_tool_use,
                ..
            },
        ) => !disable_parallel_tool_use.unwrap_or(false),
        Some(AnthropicToolChoice::None) | None => true,
    }
}

/// Translates thinking controls and schema format at the protocol boundary.
fn options(
    thinking: Option<Thinking>,
    output: Option<AnthropicOutputConfig>,
    kwargs: Option<HashMap<String, Value>>,
) -> Result<(ChatOptions, bool), AnthropicApiError> {
    let mut options = ChatOptions {
        template_kwargs: kwargs.unwrap_or_default(),
        ..Default::default()
    };
    let mut show_thinking = true;
    if let Some(thinking) = thinking {
        let (enabled, display) = match thinking {
            Thinking::Disabled => (false, None),
            Thinking::Adaptive { display } => (true, display),
            Thinking::Enabled {
                budget_tokens,
                display,
            } => {
                if budget_tokens.is_some() {
                    return Err(AnthropicApiError::invalid(
                        "A separate thinking token budget is not supported; use adaptive thinking",
                    ));
                }
                (true, display)
            }
        };
        show_thinking = enabled && !matches!(display, Some(ThinkingDisplay::Omitted));
        for key in ["thinking", "enable_thinking"] {
            if options
                .template_kwargs
                .get(key)
                .is_some_and(|value| value.as_bool() != Some(enabled))
            {
                return Err(AnthropicApiError::invalid(
                    "Conflicting thinking and chat_template_kwargs",
                ));
            }
        }
        options
            .template_kwargs
            .insert("enable_thinking".into(), json!(enabled));
    }
    if let Some(output) = output {
        options.reasoning_effort = output.effort.map(Into::into);
        if let Some(OutputFormat::JsonSchema { schema }) = output.format {
            if !schema.is_object() {
                return Err(AnthropicApiError::invalid(
                    "Output schema must be an object",
                ));
            }
            options.response_format = Some(
                json!({"type":"json_schema", "json_schema":{"name":"response", "schema":schema, "strict":true}}),
            );
        }
    }
    Ok((options, show_thinking))
}

/// Accepts explicit preservation of thinking; destructive server-side edits need their own owner.
fn validate_context_management(value: Option<&Value>) -> Result<(), AnthropicApiError> {
    let Some(value) = value else {
        return Ok(());
    };
    let Some(object) = value.as_object() else {
        return Err(AnthropicApiError::invalid("Invalid context_management"));
    };
    let Some(edits) = object.get("edits").and_then(Value::as_array) else {
        return Err(AnthropicApiError::invalid(
            "context_management requires edits",
        ));
    };
    if object.len() != 1
        || edits.iter().any(|edit| {
            edit.get("type").and_then(Value::as_str) != Some("clear_thinking_20251015")
                || edit.get("keep").and_then(Value::as_str) != Some("all")
                || edit.as_object().is_none_or(|object| object.len() != 2)
        })
    {
        return Err(AnthropicApiError::invalid(
            "Only preservation of all thinking is supported",
        ));
    }
    Ok(())
}
