// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request lowering and output assembly adapted from vLLM PR #53380,
//! commit 9de2bc119009c3e37e36ddbe240c5647d848f752.

use super::error::{ApiError, bail_invalid_request};
use super::tools::{ToolNames, qualified};
use super::types::*;
use foretoken_chat::{
    AssistantContentBlock, AssistantToolCall, ChatContent, ChatContentPart, ChatMessage,
    ChatOptions, ChatRequest, ChatToolChoice, GenerationPromptMode, ReasoningEffort,
    ResolvedToolContext,
};
use foretoken_text::{SamplingParams, TextDecodeOptions};
use serde_json::{Value, json};
use uuid::Uuid;

/// Public response metadata retained independently of the full input history.
pub(super) struct ResponseMeta {
    pub model: String,
    pub instructions: Option<String>,
    pub metadata: Option<Value>,
    pub tools: Vec<Value>,
    pub tool_choice: Value,
    pub parallel_tool_calls: bool,
    pub max_output_tokens: Option<u32>,
    pub reasoning: Option<ResponsesReasoning>,
    pub temperature: Option<f32>,
    pub top_p: Option<f32>,
    pub text: Option<ResponseTextConfig>,
    pub include_reasoning: bool,
    pub tool_names: ToolNames,
}

/// Lower one validated protocol request into the shared chat pipeline.
pub(super) fn prepare_responses_request(
    request: ResponsesRequest,
    request_id: String,
) -> Result<(ChatRequest, ResponseMeta), ApiError> {
    validate_request(&request)?;
    let model = request
        .model
        .clone()
        .filter(|s| !s.is_empty())
        .ok_or_else(|| ApiError::invalid_request("model is required", Some("model")))?;
    let (messages, continue_final) = convert_input(&request.instructions, request.input)?;
    let (converted_tools, tool_names) = ToolNames::lower(&request.tools)?;
    let requested_choice_echo = request
        .tool_choice
        .as_ref()
        .map(|choice| serde_json::to_value(choice).expect("tool choice serialization"));
    let requested_tool_choice =
        normalize_tool_choice(request.tool_choice, converted_tools.is_empty())?;
    let tool_context = ResolvedToolContext::new(
        &messages,
        converted_tools,
        requested_tool_choice,
        request.parallel_tool_calls.unwrap_or(true),
    )
    .map_err(|e| ApiError::invalid_request(e.to_string(), Some("tools")))?;
    let tool_choice = requested_choice_echo.unwrap_or_else(|| echo_tool_choice(&tool_context));
    let reasoning_effort = request.reasoning.as_ref().and_then(|r| r.effort);
    let mut template_kwargs = request.chat_template_kwargs.unwrap_or_default();
    if let Some(effort) = reasoning_effort {
        let enabled = !matches!(effort, ReasoningEffort::None);
        if template_kwargs
            .get("enable_thinking")
            .is_some_and(|v| v != &Value::Bool(enabled))
        {
            bail_invalid_request!(
                param = "reasoning",
                "reasoning.effort conflicts with enable_thinking"
            );
        }
        template_kwargs.insert("enable_thinking".into(), Value::Bool(enabled));
    }
    let mut sampling_params = SamplingParams {
        temperature: request.temperature,
        top_p: request.top_p,
        top_k: request.top_k,
        seed: request.seed,
        max_tokens: request.max_output_tokens,
        min_tokens: request.min_tokens,
        frequency_penalty: request.frequency_penalty,
        presence_penalty: request.presence_penalty,
        repetition_penalty: request.repetition_penalty,
        stop_token_ids: request.stop_token_ids,
        ignore_eos: request.ignore_eos,
        ..Default::default()
    };
    let response_format = match request.text.as_ref().and_then(|t| t.format.as_ref()) {
        None | Some(ResponseTextFormat::Text) => None,
        Some(ResponseTextFormat::JsonObject) => {
            sampling_params.structured_outputs = Some(foretoken_engine_core_client::protocol::structured_outputs::StructuredOutputsParams::json_object());
            Some(json!({"type":"json_object"}))
        }
        Some(ResponseTextFormat::JsonSchema {
            name,
            description,
            schema,
            strict,
        }) => {
            sampling_params.structured_outputs = Some(foretoken_engine_core_client::protocol::structured_outputs::StructuredOutputsParams::json(schema.clone()));
            Some(
                json!({"type":"json_schema","json_schema":{"name":name,"description":description,"schema":schema,"strict":strict}}),
            )
        }
    };
    let stop_strings = match request.stop {
        None => Vec::new(),
        Some(Value::String(s)) => vec![s],
        Some(Value::Array(values)) => values
            .into_iter()
            .map(|v| {
                v.as_str().map(str::to_owned).ok_or_else(|| {
                    ApiError::invalid_request("stop entries must be strings", Some("stop"))
                })
            })
            .collect::<Result<_, _>>()?,
        Some(_) => {
            return Err(ApiError::invalid_request(
                "stop must be a string or string array",
                Some("stop"),
            ));
        }
    };
    let chat = ChatRequest {
        request_id,
        messages,
        sampling_params,
        chat_options: ChatOptions {
            generation_prompt_mode: if continue_final {
                GenerationPromptMode::ContinueFinalAssistant
            } else {
                GenerationPromptMode::StartNewAssistant
            },
            reasoning_effort,
            response_format,
            template_kwargs,
            ..Default::default()
        },
        tool_context,
        decode_options: TextDecodeOptions {
            stop_strings: Some(stop_strings),
            skip_special_tokens: request.skip_special_tokens,
            include_stop_str_in_output: request.include_stop_str_in_output,
            ..Default::default()
        },
        intermediate: request.stream,
        priority: request.priority.unwrap_or(0),
        documents: None,
        cache_salt: request.cache_salt,
        add_special_tokens: false,
        data_parallel_rank: None,
        session_id: request.session_id,
        lora_request: None,
    };
    chat.validate()
        .map_err(|e| ApiError::invalid_request(e.to_string(), Some("input")))?;
    let meta = ResponseMeta {
        model,
        instructions: request.instructions,
        metadata: request.metadata,
        tools: request.tools,
        tool_choice,
        parallel_tool_calls: chat.parallel_tool_calls(),
        max_output_tokens: request.max_output_tokens,
        reasoning: request.reasoning,
        temperature: request.temperature,
        top_p: request.top_p,
        text: request.text,
        include_reasoning: request.include_reasoning,
        tool_names,
    };
    Ok((chat, meta))
}

/// Reject semantic fields the stateless adapter cannot execute.
fn validate_request(request: &ResponsesRequest) -> Result<(), ApiError> {
    if request.store == Some(true) {
        bail_invalid_request!(
            param = "store",
            "Response storage is not supported; use store=false and replay the conversation"
        );
    }
    if request.background == Some(true) {
        bail_invalid_request!(
            param = "background",
            "Background responses are not supported"
        );
    }
    if request.previous_response_id.is_some() {
        bail_invalid_request!(
            param = "previous_response_id",
            "Server-side continuation is not supported; provide the full input history"
        );
    }
    if request.prompt.is_some() {
        bail_invalid_request!(param = "prompt", "Stored prompts are not supported");
    }
    if request.max_tool_calls.is_some() {
        bail_invalid_request!(
            param = "max_tool_calls",
            "Server-side tool execution is not supported"
        );
    }
    if request.top_logprobs.is_some() {
        bail_invalid_request!(
            param = "top_logprobs",
            "Responses output logprobs are not supported"
        );
    }
    if request
        .truncation
        .as_deref()
        .is_some_and(|v| v != "disabled")
    {
        bail_invalid_request!(
            param = "truncation",
            "Only truncation=disabled is supported"
        );
    }
    if request
        .service_tier
        .as_deref()
        .is_some_and(|v| v != "auto" && v != "default")
    {
        bail_invalid_request!(
            param = "service_tier",
            "Requested service tier is not supported"
        );
    }
    if let Some((name, _)) = request.extra.iter().find(|(name, _)| {
        !matches!(
            name.as_str(),
            "prompt_cache_key" | "safety_identifier" | "client_metadata"
        )
    }) {
        return Err(ApiError::invalid_request(
            format!("Unsupported Responses parameter: {name}"),
            Some(name),
        ));
    }
    if request
        .include
        .as_ref()
        .is_some_and(|items| items.iter().any(|v| v != "reasoning.encrypted_content"))
    {
        bail_invalid_request!(
            param = "include",
            "Requested include fields are not supported"
        );
    }
    if request
        .reasoning
        .as_ref()
        .and_then(|r| r.summary.as_deref())
        .is_some_and(|s| s != "auto")
    {
        bail_invalid_request!(
            param = "reasoning.summary",
            "Only automatic reasoning output is supported"
        );
    }
    if request
        .text
        .as_ref()
        .and_then(|t| t.verbosity.as_ref())
        .is_some()
    {
        bail_invalid_request!(param = "text.verbosity", "Text verbosity is not supported");
    }
    if request.logit_bias.is_some()
        || request.structured_outputs.is_some()
        || request.kv_transfer_params.is_some()
        || request.ec_transfer_params.is_some()
        || request.vllm_xargs.is_some()
    {
        bail_invalid_request!(
            param = "input",
            "Internal engine overrides are not supported by this endpoint"
        );
    }
    Ok(())
}

/// Resolve the requested tool choice, mirroring the Python
/// `check_tool_usage` validator: without tools, named function choices are
/// errors; with tools, named choices must exist (enforced by
/// [`ResolvedToolContext`]).
fn normalize_tool_choice(
    tool_choice: Option<super::types::ResponseToolChoice>,
    tools_empty: bool,
) -> Result<Option<ChatToolChoice>, ApiError> {
    use super::types::ResponseToolChoice as Choice;

    match tool_choice {
        None => Ok(None),
        Some(Choice::Mode(mode)) => match mode.as_str() {
            "none" => Ok(Some(ChatToolChoice::None)),
            "auto" => Ok(Some(ChatToolChoice::Auto)),
            "required" => Ok(Some(ChatToolChoice::Required)),
            _ => Err(ApiError::invalid_request(
                "Unknown tool_choice mode",
                Some("tool_choice"),
            )),
        },
        Some(Choice::Object(value))
            if value
                .get("type")
                .and_then(Value::as_str)
                .is_some_and(|kind| matches!(kind, "function" | "custom")) =>
        {
            if tools_empty {
                bail_invalid_request!(
                    param = "tool_choice",
                    "Tool choice 'function' not found in 'tools' parameter."
                );
            }
            let Some(name) = value.get("name").and_then(Value::as_str) else {
                bail_invalid_request!(
                    param = "tool_choice",
                    "Function tool choice requires a 'name' field."
                );
            };
            Ok(Some(ChatToolChoice::Function {
                name: qualified(value.get("namespace").and_then(Value::as_str), name),
            }))
        }
        Some(Choice::Object(value)) => {
            let tool_type = value
                .get("type")
                .and_then(Value::as_str)
                .unwrap_or("<missing type>");
            bail_invalid_request!(
                param = "tool_choice",
                "Tool choice type '{tool_type}' is not supported by this frontend."
            );
        }
    }
}

/// Echoed tool choice in the response, after resolution: without tools the
/// resolved choice is always `none` (Python parity).
fn echo_tool_choice(tool_context: &ResolvedToolContext) -> Value {
    match &tool_context.tool_choice {
        ChatToolChoice::None => Value::String("none".to_string()),
        ChatToolChoice::Auto => Value::String("auto".to_string()),
        ChatToolChoice::Required => Value::String("required".to_string()),
        ChatToolChoice::Function { name } => {
            serde_json::json!({"type": "function", "name": name})
        }
    }
}

/// Convert the request input into chat messages.
///
/// Returns the messages and whether generation should continue the final
/// assistant message (Anthropic-style partial completion), mirroring
/// `should_continue_final_message` in the Python frontend.
fn convert_input(
    instructions: &Option<String>,
    input: ResponsesInput,
) -> Result<(Vec<ChatMessage>, bool), ApiError> {
    let mut messages = Vec::new();
    if let Some(instructions) = instructions
        && !instructions.is_empty()
    {
        messages.push(ChatMessage::system(instructions.clone()));
    }

    let items = match input {
        ResponsesInput::Text(text) => {
            messages.push(ChatMessage::user(text));
            return Ok((messages, false));
        }
        ResponsesInput::Items(items) => items,
    };

    let continue_final = should_continue_final_message(&items);

    let mut assistant = AssistantTurn::default();
    for (index, raw) in items.iter().enumerate() {
        match parse_input_item(index, raw)? {
            ResponseInputItem::Message(message) => match message.role.as_str() {
                "system" | "developer" | "user" => {
                    assistant.flush(&mut messages);
                    let content =
                        convert_content_parts(message.role.as_str(), message.content, true)?;
                    messages.push(match message.role.as_str() {
                        "system" => ChatMessage::system(content),
                        "developer" => ChatMessage::developer(content, None),
                        _ => ChatMessage::user(content),
                    });
                }
                "assistant" => {
                    let content = convert_content_parts("assistant", message.content, false)?;
                    let text = content.try_flatten_to_text().map_err(|_| {
                        ApiError::invalid_request(
                            "assistant input items only support text content".to_string(),
                            Some("input"),
                        )
                    })?;
                    assistant.push_text(&mut messages, text);
                }
                role => {
                    bail_invalid_request!(
                        param = "input",
                        "unsupported role '{role}' in message input item"
                    );
                }
            },
            ResponseInputItem::FunctionCall(call) => {
                assistant.push_block(AssistantContentBlock::ToolCall(AssistantToolCall {
                    id: call.call_id,
                    name: call.name,
                    arguments: call.arguments,
                }));
            }
            ResponseInputItem::FunctionCallOutput(output) => {
                assistant.flush(&mut messages);
                messages.push(ChatMessage::tool_response(
                    output.output.flatten_text()?,
                    output.call_id,
                ));
            }
            ResponseInputItem::Reasoning(reasoning) => {
                if reasoning
                    .encrypted_content
                    .as_ref()
                    .is_some_and(|value| !value.is_empty())
                {
                    bail_invalid_request!(
                        param = "input",
                        "Encrypted reasoning content is not supported by this frontend."
                    );
                }
                let text = reasoning_content_string(&reasoning);
                if !text.is_empty() {
                    assistant.push_reasoning(&mut messages, text);
                }
            }
        }
    }
    assistant.flush(&mut messages);

    Ok((messages, continue_final))
}

/// Parse one raw input item value into a typed input item, inserting
/// `type: "message"` when a message-shaped item omits it (mirroring the
/// Python `input_item_parsing` model validator).
fn parse_input_item(index: usize, raw: &Value) -> Result<ResponseInputItem, ApiError> {
    let Some(object) = raw.as_object() else {
        bail_invalid_request!(
            param = "input",
            "input item at index {index} must be an object"
        );
    };

    let item_type = object.get("type").and_then(Value::as_str);
    let effective_type = match (item_type, object.get("role").and_then(Value::as_str)) {
        (None, Some(_)) => "message",
        (item_type, _) => item_type.unwrap_or_default(),
    };
    match effective_type {
        "message"
        | "function_call"
        | "function_call_output"
        | "custom_tool_call"
        | "custom_tool_call_output"
        | "reasoning" => {}
        "item_reference" => {
            bail_invalid_request!(
                param = "previous_response_id",
                "item_reference input items require the Responses API store, which is \
                 not supported by this frontend."
            );
        }
        "" => {
            bail_invalid_request!(
                param = "input",
                "input item at index {index} is missing required field 'type' (or 'role' \
                 for message items)."
            );
        }
        other => {
            bail_invalid_request!(
                param = "input",
                "input item at index {index} has unsupported type '{other}'."
            );
        }
    }

    let mut value = raw.clone();
    if item_type.is_none()
        && let Some(object) = value.as_object_mut()
    {
        object.insert("type".to_string(), Value::String("message".to_string()));
    }
    if let Some(object) = value.as_object_mut() {
        if matches!(effective_type, "function_call" | "custom_tool_call") {
            let name = object.get("name").and_then(Value::as_str).ok_or_else(|| {
                ApiError::invalid_request("Tool call name is required", Some("input"))
            })?;
            let internal = qualified(object.get("namespace").and_then(Value::as_str), name);
            object.insert("name".into(), Value::String(internal));
        }
        if effective_type == "custom_tool_call" {
            let input = object
                .remove("input")
                .filter(Value::is_string)
                .ok_or_else(|| {
                    ApiError::invalid_request("Custom tool input must be text", Some("input"))
                })?;
            object.insert(
                "arguments".into(),
                Value::String(json!({"input":input}).to_string()),
            );
            object.insert("type".into(), json!("function_call"));
        } else if effective_type == "custom_tool_call_output" {
            object.insert("type".into(), json!("function_call_output"));
        }
    }
    serde_json::from_value(value).map_err(|error| {
        ApiError::invalid_request(
            format!("failed to parse input item at index {index}: {error}"),
            Some("input"),
        )
    })
}

/// Convert message content into internal chat content.
fn convert_content_parts(
    role: &str,
    content: ResponseMessageContent,
    allow_multimodal: bool,
) -> Result<ChatContent, ApiError> {
    match content {
        ResponseMessageContent::Text(text) => Ok(ChatContent::Text(text)),
        ResponseMessageContent::Parts(parts) => {
            let mut converted = Vec::with_capacity(parts.len());
            for part in parts {
                match part {
                    ResponseInputContentPart::InputText { text }
                    | ResponseInputContentPart::OutputText { text, .. } => {
                        converted.push(ChatContentPart::text(text));
                    }
                    ResponseInputContentPart::Refusal { refusal } => {
                        // Refusals carry refusable-answer text only; keep it
                        // so templates still see the turn.
                        converted.push(ChatContentPart::text(refusal));
                    }
                    ResponseInputContentPart::InputImage {
                        image_url,
                        detail,
                        file_id,
                    } => {
                        if !allow_multimodal {
                            bail_invalid_request!(
                                param = "input",
                                "input_image parts are not supported for role '{role}'."
                            );
                        }
                        if file_id.is_some() {
                            bail_invalid_request!(
                                param = "input",
                                "input_image parts with file_id are not supported; pass \
                                 an image_url instead."
                            );
                        }
                        let Some(image_url) = image_url else {
                            bail_invalid_request!(
                                param = "input",
                                "input_image parts require 'image_url'."
                            );
                        };
                        if !image_url.starts_with("data:image/") || !image_url.contains(";base64,")
                        {
                            bail_invalid_request!(
                                param = "input",
                                "Images require inline base64 data URLs"
                            );
                        }
                        converted.push(ChatContentPart::ImageUrl {
                            image_url,
                            detail: detail.map(serde_json::from_value).transpose().map_err(
                                |_| {
                                    ApiError::invalid_request("Invalid image detail", Some("input"))
                                },
                            )?,
                            uuid: None,
                        });
                    }
                    ResponseInputContentPart::InputAudio { .. } => {
                        bail_invalid_request!(param = "input", "Audio input is not supported");
                    }
                    ResponseInputContentPart::InputFile { .. } => {
                        bail_invalid_request!(
                            param = "input",
                            "input_file parts are not supported by this frontend."
                        );
                    }
                }
            }
            Ok(ChatContent::Parts(converted))
        }
    }
}

/// Accumulates content blocks across consecutive input items belonging to one
/// assistant turn, flushing at turn boundaries.
///
/// Merging rules mirror `_construct_message_from_response_item` in the Python
/// frontend: a text item starts a new assistant turn when the pending turn
/// already has visible text, a reasoning item starts a new turn when the
/// pending turn already has reasoning, and function calls always merge.
#[derive(Default)]
struct AssistantTurn {
    blocks: Vec<AssistantContentBlock>,
}

impl AssistantTurn {
    fn push_text(&mut self, messages: &mut Vec<ChatMessage>, text: String) {
        if self
            .blocks
            .iter()
            .any(|block| matches!(block, AssistantContentBlock::Text { .. }))
        {
            self.flush(messages);
        }
        self.push_block(AssistantContentBlock::Text { text });
    }

    fn push_reasoning(&mut self, messages: &mut Vec<ChatMessage>, text: String) {
        if self
            .blocks
            .iter()
            .any(|block| matches!(block, AssistantContentBlock::Reasoning { .. }))
        {
            self.flush(messages);
        }
        self.push_block(AssistantContentBlock::Reasoning { text });
    }

    fn push_block(&mut self, block: AssistantContentBlock) {
        self.blocks.push(block);
    }

    /// Flush the pending assistant turn into the message list if non-empty.
    fn flush(&mut self, messages: &mut Vec<ChatMessage>) {
        if self.blocks.is_empty() {
            return;
        }
        messages.push(ChatMessage::assistant_blocks(std::mem::take(
            &mut self.blocks,
        )));
    }
}

/// Extract the reasoning text from one reasoning input item, mirroring the
/// Python converter: prefer full content and concatenate every returned part.
fn reasoning_content_string(reasoning: &ResponseInputReasoning) -> String {
    reasoning
        .content
        .as_ref()
        .or(reasoning.summary.as_ref())
        .map(|parts| parts.iter().map(TextPart::text).collect())
        .unwrap_or_default()
}

impl ResponseMessageContent {
    /// Flatten content into one plain string without separators, rejecting
    /// non-text parts (tool outputs support text only in this frontend).
    fn flatten_text(&self) -> Result<String, ApiError> {
        match self {
            Self::Text(text) => Ok(text.clone()),
            Self::Parts(parts) => {
                let mut flattened = String::new();
                for part in parts {
                    match part {
                        ResponseInputContentPart::InputText { text }
                        | ResponseInputContentPart::OutputText { text, .. } => {
                            flattened.push_str(text)
                        }
                        other => {
                            bail_invalid_request!(
                                param = "input",
                                "function_call_output only supports text content parts; got \
                                 part type '{}'",
                                part_type_name(other)
                            );
                        }
                    }
                }
                Ok(flattened)
            }
        }
    }
}

/// Build the response `output` items from the collected assistant message.
///
/// Mirrors `build_response_output_items` in the Python frontend: reasoning
/// comes first when present and enabled, then visible text, then tool calls —
/// except items here follow the structured block order, which preserves
/// interleaved text/tool-call ordering when the parser yields it.
pub(crate) fn build_output_items(
    message: &foretoken_chat::AssistantMessage,
    include_reasoning: bool,
    status: ResponseItemStatus,
    tool_names: &ToolNames,
) -> Vec<ResponseOutputItem> {
    let truncated = status == ResponseItemStatus::Incomplete;
    message
        .content
        .iter()
        .enumerate()
        .filter_map(|(index, block)| {
            let item_status = if truncated && index + 1 == message.content.len() {
                ResponseItemStatus::Incomplete
            } else {
                ResponseItemStatus::Completed
            };
            match block {
                AssistantContentBlock::Reasoning { text } if include_reasoning => {
                    Some(ResponseOutputItem::Reasoning {
                        id: format!("rs_{}", Uuid::new_v4().simple()),
                        summary: vec![],
                        content: Some(vec![TextPart::reasoning_text(text.clone())]),
                        status: Some(item_status),
                    })
                }
                AssistantContentBlock::Reasoning { .. } => None,
                AssistantContentBlock::Text { text } if !text.is_empty() => {
                    Some(ResponseOutputItem::Message {
                        id: format!("msg_{}", Uuid::new_v4().simple()),
                        role: AssistantRole,
                        status: item_status,
                        content: vec![ResponseOutputContentPart::OutputText {
                            text: text.clone(),
                            annotations: vec![],
                            logprobs: None,
                        }],
                    })
                }
                AssistantContentBlock::Text { .. } => None,
                AssistantContentBlock::ToolCall(call) => {
                    let complete = tool_names.complete_arguments(&call.name, &call.arguments);
                    if !complete && tool_names.get(&call.name).is_some_and(|tool| tool.custom) {
                        return None;
                    }
                    Some(ResponseOutputItem::FunctionCall {
                        id: format!("fc_{}", Uuid::new_v4().simple()),
                        call_id: tool_call_id(call),
                        name: call.name.clone(),
                        arguments: call.arguments.clone(),
                        status: Some(if truncated && !complete {
                            ResponseItemStatus::Incomplete
                        } else {
                            ResponseItemStatus::Completed
                        }),
                    })
                }
            }
        })
        .collect()
}

/// Pick the wire `call_id` for one parsed tool call, generating one when the
/// parser did not assign an ID (Python generates `make_tool_call_id`).
fn tool_call_id(call: &AssistantToolCall) -> String {
    if call.id.is_empty() {
        format!("call_{}", Uuid::new_v4().simple())
    } else {
        call.id.clone()
    }
}

/// Return the wire `type` name of one input content part for error messages.
fn part_type_name(part: &ResponseInputContentPart) -> &'static str {
    match part {
        ResponseInputContentPart::InputText { .. } => "input_text",
        ResponseInputContentPart::InputImage { .. } => "input_image",
        ResponseInputContentPart::InputAudio { .. } => "input_audio",
        ResponseInputContentPart::InputFile { .. } => "input_file",
        ResponseInputContentPart::OutputText { .. } => "output_text",
        ResponseInputContentPart::Refusal { .. } => "refusal",
    }
}

/// Determine whether the final input item is a partial assistant message or
/// reasoning item that generation should continue.
///
/// Mirrors `should_continue_final_message` in the Python frontend.
fn should_continue_final_message(items: &[Value]) -> bool {
    let Some(last) = items.last() else {
        return false;
    };
    let status = last.get("status").and_then(Value::as_str);
    if !matches!(status, Some("in_progress" | "incomplete")) {
        return false;
    }
    match last.get("type").and_then(Value::as_str) {
        Some("reasoning") => true,
        Some("message") => last.get("role").and_then(Value::as_str) == Some("assistant"),
        // Type-less items with a role are messages.
        None => last.get("role").and_then(Value::as_str) == Some("assistant"),
        _ => false,
    }
}

/// Build standard Responses usage from the shared engine counters.
pub(super) fn build_usage(usage: &vllm_llm::TokenUsage) -> ResponseUsage {
    ResponseUsage {
        input_tokens: usage.prompt_token_count,
        input_tokens_details: InputTokensDetails {
            cached_tokens: usage.cached_token_count,
        },
        output_tokens: usage.output_token_count,
        output_tokens_details: OutputTokensDetails {
            // Match vLLM/SGLang's zero default when reasoning tokens are not counted separately.
            reasoning_tokens: 0,
        },
        total_tokens: usage.prompt_token_count + usage.output_token_count,
    }
}

/// Assemble the same response envelope for collected JSON and lifecycle SSE.
pub(super) fn build_response(
    meta: &ResponseMeta,
    request_id: &str,
    created_at: u64,
    output: Vec<ResponseOutputItem>,
    status: ResponseItemStatus,
    usage: Option<ResponseUsage>,
) -> ResponsesResponse {
    ResponsesResponse {
        id: request_id.into(),
        object: ResponseObject,
        created_at,
        status,
        background: false,
        store: false,
        error: None,
        incomplete_details: (status == ResponseItemStatus::Incomplete).then(|| IncompleteDetails {
            reason: "max_output_tokens".into(),
        }),
        instructions: meta.instructions.clone(),
        max_output_tokens: meta.max_output_tokens,
        max_tool_calls: None,
        metadata: meta.metadata.clone(),
        model: meta.model.clone(),
        output,
        parallel_tool_calls: meta.parallel_tool_calls,
        previous_response_id: None,
        prompt: None,
        reasoning: meta.reasoning.clone(),
        service_tier: "default".into(),
        temperature: meta.temperature,
        text: meta.text.clone(),
        tool_choice: meta.tool_choice.clone(),
        tools: meta.tools.clone(),
        top_p: meta.top_p,
        top_logprobs: None,
        truncation: "disabled".into(),
        usage,
        user: None,
        presence_penalty: None,
        frequency_penalty: None,
    }
}
