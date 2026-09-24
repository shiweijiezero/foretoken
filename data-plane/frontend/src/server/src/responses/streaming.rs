// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Responses SSE adapted from vLLM PR #53380, commit
//! 9de2bc119009c3e37e36ddbe240c5647d848f752.
//!
//! Maps the structured `vllm-chat` event stream (`ChatEvent`) onto the OpenAI
//! Responses SSE event protocol. Event shapes and emission order mirror the
//! simple (non-Harmony) streaming path of the Python frontend in
//! `vllm/entrypoints/openai/responses/streaming_events.py`. Item IDs and order
//! remain identical in streamed events and the terminal response.

use foretoken_chat::{AssistantBlockKind, AssistantContentBlock, AssistantToolCall, ChatEvent};
use serde_json::{Map, Value};
use uuid::Uuid;

use super::error::ApiError;
use super::tools::ToolNames;
use super::types::{
    AssistantRole, ResponseItemStatus, ResponseOutputContentPart, ResponseOutputItem, TextPart,
};

/// One Responses API SSE event.
///
/// The flat wire shape carries `type` and `sequence_number` next to the
/// event-specific payload (`response`, `item`, `part`, ...), matching how the
/// Python frontend serializes its typed SDK events. `sequence_number` is
/// assigned centrally by the SSE encoder for all events of one request.
#[derive(Debug, Clone, PartialEq)]
pub(crate) struct ResponseStreamEvent {
    /// The wire `type` string (e.g. `response.output_text.delta`).
    event_type: &'static str,
    /// Event-specific payload fields, serialized flat after `type`.
    payload: Map<String, Value>,
}

impl ResponseStreamEvent {
    /// Return the wire event type (used for the SSE `event:` line).
    pub(crate) fn event_type(&self) -> &'static str {
        self.event_type
    }

    /// Serialize the full wire payload including `type` and the assigned
    /// sequence number.
    pub(crate) fn to_json(&self, sequence_number: u64) -> String {
        let mut flattened = Map::with_capacity(self.payload.len() + 2);
        flattened.insert(
            "type".to_string(),
            Value::String(self.event_type.to_string()),
        );
        flattened.insert("sequence_number".to_string(), Value::from(sequence_number));
        flattened.extend(self.payload.clone());
        serde_json::to_string(&Value::Object(flattened))
            .expect("stream event payload must serialize to JSON")
    }
}

/// Build one lifecycle event (`response.created`, `response.in_progress`,
/// `response.completed`, `response.failed`).
pub(crate) fn response_lifecycle_event(
    event_type: &'static str,
    response: &super::types::ResponsesResponse,
) -> ResponseStreamEvent {
    let response = serde_json::to_value(response).expect("response must serialize to JSON");
    ResponseStreamEvent {
        event_type,
        payload: Map::from_iter([("response".to_string(), response)]),
    }
}

/// State machine that maps one `vllm-chat` event stream onto Responses API
/// SSE events.
///
/// Items open lazily on the first non-empty delta so empty blocks never
/// appear in the stream, matching the Python state machine. Item IDs assigned
/// here are matched back onto the final response's output items in
/// [`Self::final_output_items`] so streamed events and the terminal payload
/// agree about IDs.
pub(crate) struct OutputItemStreamer {
    /// Index of the next output item.
    output_index: u32,
    /// The currently open output item, if any.
    current: Option<OpenItem>,
    /// Whether reasoning items appear in both streamed and collected output.
    include_reasoning: bool,
    /// Finalized items are the authority for the terminal response payload.
    completed_items: Vec<ResponseOutputItem>,
}

/// The currently streamed assistant output item.
enum OpenItem {
    Reasoning {
        item_id: String,
        text: String,
    },
    Message {
        item_id: String,
        text: String,
    },
    FunctionCall {
        item_id: String,
        call_id: String,
        name: String,
        arguments: String,
        saw_delta: bool,
    },
}

impl OutputItemStreamer {
    pub(crate) fn new(include_reasoning: bool) -> Self {
        Self {
            output_index: 0,
            current: None,
            include_reasoning,
            completed_items: Vec::new(),
        }
    }

    /// Process one chat event, returning the SSE events to emit.
    pub(crate) fn on_event(
        &mut self,
        event: &ChatEvent,
        names: &ToolNames,
    ) -> Vec<ResponseStreamEvent> {
        match event {
            ChatEvent::Start { .. } | ChatEvent::LogprobsDelta { .. } | ChatEvent::Done { .. } => {
                vec![]
            }
            ChatEvent::BlockStart { .. } => {
                self.close_current(None, ResponseItemStatus::Completed, names)
            }
            ChatEvent::BlockDelta { kind, delta, .. } => {
                if delta.is_empty()
                    || (*kind == AssistantBlockKind::Reasoning && !self.include_reasoning)
                {
                    return vec![];
                }
                match kind {
                    AssistantBlockKind::Reasoning => {
                        let mut events = self.open_reasoning_if_needed();
                        events.push(self.reasoning_delta(delta.clone()));
                        events
                    }
                    AssistantBlockKind::Text => {
                        let mut events = self.open_message_if_needed();
                        events.push(self.text_delta(delta.clone()));
                        events
                    }
                    // Tool-calls flow through the dedicated events.
                    AssistantBlockKind::ToolCall => vec![],
                }
            }
            ChatEvent::BlockEnd { block, .. } => {
                // A block boundary alone does not distinguish a normal stop from budget exhaustion.
                if let Some(OpenItem::Reasoning { text, .. } | OpenItem::Message { text, .. }) =
                    self.current.as_mut()
                    && let Some(final_text) = block_text(block)
                {
                    *text = final_text.to_owned();
                }
                vec![]
            }
            ChatEvent::ToolCallStart { id, name, .. } => {
                let mut events = self.close_current(None, ResponseItemStatus::Completed, names);
                let added = self.open_function_call(id, name);
                // Custom items have no incomplete status. Publish them only after their wrapper is complete.
                if !names.get(name).is_some_and(|tool| tool.custom) {
                    events.push(added);
                }
                events
            }
            ChatEvent::ToolCallArgumentsDelta { delta, .. } => {
                if delta.is_empty() {
                    return vec![];
                }
                let event = self.function_call_delta(delta.clone());
                if matches!(self.current.as_ref(), Some(OpenItem::FunctionCall { name, .. })
                    if names.get(name).is_some_and(|tool| tool.custom))
                {
                    vec![]
                } else {
                    vec![event]
                }
            }
            ChatEvent::ToolCallEnd { call, .. } => {
                self.close_current(Some(call), ResponseItemStatus::Completed, names)
            }
        }
    }

    /// Resolve deferred function completion and the last text item using the actual termination.
    pub(crate) fn finish(
        &mut self,
        status: ResponseItemStatus,
        names: &ToolNames,
    ) -> Vec<ResponseStreamEvent> {
        let mut events = Vec::new();
        if status == ResponseItemStatus::Completed {
            for (index, item) in self.completed_items.iter_mut().enumerate() {
                if let ResponseOutputItem::FunctionCall {
                    status: Some(item_status),
                    ..
                } = item
                    && *item_status == ResponseItemStatus::Incomplete
                {
                    *item_status = ResponseItemStatus::Completed;
                    events.extend(function_call_done_events(index as u32, item));
                }
            }
        }
        events.extend(self.close_current(None, status, names));
        events
    }

    /// Return output with the same public order and IDs, including non-dispatchable partial calls.
    pub(crate) fn final_output_items(&self) -> Vec<ResponseOutputItem> {
        self.completed_items.clone()
    }

    fn open_reasoning_if_needed(&mut self) -> Vec<ResponseStreamEvent> {
        if self.current.is_some() {
            return vec![];
        }
        let item_id = format!("rs_{}", Uuid::new_v4().simple());
        let item = ResponseOutputItem::Reasoning {
            id: item_id.clone(),
            summary: vec![],
            content: None,
            status: Some(ResponseItemStatus::InProgress),
        };
        let output_index = self.output_index;
        self.current = Some(OpenItem::Reasoning {
            item_id: item_id.clone(),
            text: String::new(),
        });
        vec![
            output_item_event("response.output_item.added", output_index, item),
            part_event(
                "response.reasoning_part.added",
                output_index,
                &item_id,
                [(
                    "part",
                    serde_json::json!({"type": "reasoning_text", "text": ""}),
                )],
            ),
        ]
    }

    fn open_message_if_needed(&mut self) -> Vec<ResponseStreamEvent> {
        if self.current.is_some() {
            return vec![];
        }
        let item_id = format!("msg_{}", Uuid::new_v4().simple());
        let item = ResponseOutputItem::Message {
            id: item_id.clone(),
            role: AssistantRole,
            status: ResponseItemStatus::InProgress,
            content: vec![],
        };
        let output_index = self.output_index;
        self.current = Some(OpenItem::Message {
            item_id: item_id.clone(),
            text: String::new(),
        });
        vec![
            output_item_event("response.output_item.added", output_index, item),
            content_part_added(output_index, &item_id),
        ]
    }

    fn open_function_call(&mut self, id: &str, name: &str) -> ResponseStreamEvent {
        let item_id = format!("fc_{}", Uuid::new_v4().simple());
        let call_id = if id.is_empty() {
            format!("call_{}", Uuid::new_v4().simple())
        } else {
            id.to_string()
        };
        let output_index = self.output_index;
        self.current = Some(OpenItem::FunctionCall {
            item_id: item_id.clone(),
            call_id: call_id.clone(),
            name: name.to_string(),
            arguments: String::new(),
            saw_delta: false,
        });
        let item = ResponseOutputItem::FunctionCall {
            id: item_id,
            call_id,
            name: name.to_string(),
            arguments: String::new(),
            status: Some(ResponseItemStatus::InProgress),
        };
        output_item_event("response.output_item.added", output_index, item)
    }

    fn reasoning_delta(&mut self, delta: String) -> ResponseStreamEvent {
        let Some(OpenItem::Reasoning { item_id, text }) = self.current.as_mut() else {
            unreachable!("reasoning delta requires an open reasoning item");
        };
        text.push_str(&delta);
        part_event(
            "response.reasoning_text.delta",
            self.output_index,
            item_id,
            [("delta", Value::String(delta))],
        )
    }

    fn text_delta(&mut self, delta: String) -> ResponseStreamEvent {
        let Some(OpenItem::Message { item_id, text }) = self.current.as_mut() else {
            unreachable!("text delta requires an open message item");
        };
        text.push_str(&delta);
        part_event(
            "response.output_text.delta",
            self.output_index,
            item_id,
            [
                ("delta", Value::String(delta)),
                ("logprobs", Value::Array(vec![])),
            ],
        )
    }

    fn function_call_delta(&mut self, delta: String) -> ResponseStreamEvent {
        let Some(OpenItem::FunctionCall {
            item_id,
            arguments,
            saw_delta,
            ..
        }) = self.current.as_mut()
        else {
            unreachable!("function call delta requires an open function call item");
        };
        arguments.push_str(&delta);
        *saw_delta = true;
        part_event(
            "response.function_call_arguments.delta",
            self.output_index,
            item_id,
            [("delta", Value::String(delta))],
        )
    }

    /// Close the current item, emitting the done event sequence.
    ///
    /// `final_call` overrides the streamed tool-call payload with the parser's
    /// final tool-call block when available.
    fn close_current(
        &mut self,
        final_call: Option<&AssistantToolCall>,
        status: ResponseItemStatus,
        names: &ToolNames,
    ) -> Vec<ResponseStreamEvent> {
        let Some(open) = self.current.take() else {
            return vec![];
        };
        let output_index = self.output_index;
        let events = match open {
            OpenItem::Reasoning { item_id, text } => {
                let part = TextPart::reasoning_text(text.clone());
                let item = ResponseOutputItem::Reasoning {
                    id: item_id.clone(),
                    summary: vec![],
                    content: Some(vec![part.clone()]),
                    status: Some(status),
                };
                self.completed_items.push(item.clone());
                vec![
                    part_event(
                        "response.reasoning_text.done",
                        output_index,
                        &item_id,
                        [("text", Value::String(text))],
                    ),
                    part_event(
                        "response.reasoning_part.done",
                        output_index,
                        &item_id,
                        [(
                            "part",
                            serde_json::to_value(part).expect("part must serialize"),
                        )],
                    ),
                    output_item_event("response.output_item.done", output_index, item),
                ]
            }
            OpenItem::Message { item_id, text } => {
                let part = ResponseOutputContentPart::OutputText {
                    text: text.clone(),
                    annotations: vec![],
                    logprobs: None,
                };
                let item = ResponseOutputItem::Message {
                    id: item_id.clone(),
                    role: AssistantRole,
                    status,
                    content: vec![part.clone()],
                };
                self.completed_items.push(item.clone());
                vec![
                    part_event(
                        "response.output_text.done",
                        output_index,
                        &item_id,
                        [
                            ("text", Value::String(text)),
                            ("logprobs", Value::Array(vec![])),
                        ],
                    ),
                    part_event(
                        "response.content_part.done",
                        output_index,
                        &item_id,
                        [(
                            "part",
                            serde_json::to_value(&part).expect("part must serialize"),
                        )],
                    ),
                    output_item_event("response.output_item.done", output_index, item),
                ]
            }
            OpenItem::FunctionCall {
                item_id,
                call_id,
                name,
                arguments,
                saw_delta,
            } => {
                let (call_id, name, arguments) = match final_call {
                    Some(call) => (
                        if call.id.is_empty() {
                            call_id
                        } else {
                            call.id.clone()
                        },
                        call.name.clone(),
                        call.arguments.clone(),
                    ),
                    None => (call_id, name, arguments),
                };
                let custom = names.get(&name).is_some_and(|tool| tool.custom);
                let complete = names.complete_arguments(&name, &arguments);
                if custom && !complete {
                    // Done retains the original call, so its finish reason can distinguish truncation from failure.
                    return vec![];
                }
                let item = ResponseOutputItem::FunctionCall {
                    id: item_id.clone(),
                    call_id: call_id.clone(),
                    name: name.clone(),
                    arguments: arguments.clone(),
                    status: Some(if complete {
                        ResponseItemStatus::Completed
                    } else {
                        ResponseItemStatus::Incomplete
                    }),
                };
                self.completed_items.push(item.clone());
                if !complete {
                    // Keep the raw partial arguments for the incomplete response, without a dispatchable done event.
                    self.output_index += 1;
                    return vec![];
                }
                let mut events = Vec::new();
                if custom {
                    events.push(output_item_event(
                        "response.output_item.added",
                        output_index,
                        ResponseOutputItem::FunctionCall {
                            id: item_id.clone(),
                            call_id,
                            name: name.clone(),
                            arguments: String::new(),
                            status: Some(ResponseItemStatus::InProgress),
                        },
                    ));
                }
                if !custom && !saw_delta && !arguments.is_empty() {
                    events.push(part_event(
                        "response.function_call_arguments.delta",
                        output_index,
                        &item_id,
                        [("delta", Value::String(arguments.clone()))],
                    ));
                }
                events.extend(function_call_done_events(output_index, &item));
                events
            }
        };
        self.output_index += 1;
        events
    }
}

/// Complete a dispatchable function item without altering its streamed identity or arguments.
fn function_call_done_events(
    output_index: u32,
    item: &ResponseOutputItem,
) -> Vec<ResponseStreamEvent> {
    let ResponseOutputItem::FunctionCall {
        id,
        name,
        arguments,
        ..
    } = item
    else {
        unreachable!("function completion requires a function item");
    };
    vec![
        part_event(
            "response.function_call_arguments.done",
            output_index,
            id,
            [
                ("arguments", Value::String(arguments.clone())),
                ("name", Value::String(name.clone())),
            ],
        ),
        output_item_event("response.output_item.done", output_index, item.clone()),
    ]
}

/// Extract the text of one text/reasoning block.
fn block_text(block: &AssistantContentBlock) -> Option<&str> {
    match block {
        AssistantContentBlock::Reasoning { text } | AssistantContentBlock::Text { text } => {
            Some(text)
        }
        AssistantContentBlock::ToolCall(_) => None,
    }
}

/// Build one `response.output_item.added`/`response.output_item.done` event.
fn output_item_event(
    event_type: &'static str,
    output_index: u32,
    item: ResponseOutputItem,
) -> ResponseStreamEvent {
    let item = serde_json::to_value(item).expect("output item must serialize to JSON");
    ResponseStreamEvent {
        event_type,
        payload: Map::from_iter([
            ("output_index".to_string(), Value::from(output_index)),
            ("item".to_string(), item),
        ]),
    }
}

/// Build one `response.content_part.added` event.
fn content_part_added(output_index: u32, item_id: &str) -> ResponseStreamEvent {
    part_event(
        "response.content_part.added",
        output_index,
        item_id,
        [(
            "part",
            serde_json::json!({
                "type": "output_text",
                "text": "",
                "annotations": [],
                "logprobs": [],
            }),
        )],
    )
}

/// Build one part-scoped event carrying the shared
/// `item_id`/`output_index`/`content_index` frame.
fn part_event<const N: usize>(
    event_type: &'static str,
    output_index: u32,
    item_id: &str,
    payload: [(&str, Value); N],
) -> ResponseStreamEvent {
    let mut fields = Map::with_capacity(N + 3);
    fields.insert("item_id".to_string(), Value::String(item_id.to_string()));
    fields.insert("output_index".to_string(), Value::from(output_index));
    fields.insert("content_index".to_string(), Value::from(0u32));
    for (key, value) in payload {
        fields.insert(key.to_string(), value);
    }
    ResponseStreamEvent {
        event_type,
        payload: fields,
    }
}

/// Restore request-local tool identities in events without exposing encoded custom arguments.
pub(super) fn restore_tool_event(
    mut event: ResponseStreamEvent,
    names: &ToolNames,
) -> Result<Vec<ResponseStreamEvent>, ApiError> {
    if let Some(item) = event.payload.get_mut("item") {
        names.restore_item(item)?;
    }
    if let Some(response) = event.payload.get_mut("response") {
        super::restore_tools(response, names)?;
    }
    let custom = event
        .payload
        .get("name")
        .and_then(Value::as_str)
        .and_then(|name| names.get(name))
        .is_some_and(|tool| tool.custom);
    if custom && event.event_type == "response.function_call_arguments.done" {
        let arguments = event
            .payload
            .remove("arguments")
            .and_then(|v| v.as_str().map(str::to_owned))
            .ok_or_else(|| ApiError::invalid_request("Missing custom tool arguments", None))?;
        let parsed: Value = serde_json::from_str(&arguments)
            .map_err(|_| ApiError::invalid_request("Invalid custom tool arguments", None))?;
        let input = parsed
            .get("input")
            .and_then(Value::as_str)
            .ok_or_else(|| ApiError::invalid_request("Custom tool input must be text", None))?;
        event.payload.remove("name");
        event.payload.remove("content_index");
        let mut delta = event.clone();
        delta.event_type = "response.custom_tool_call_input.delta";
        delta
            .payload
            .insert("delta".into(), Value::String(input.into()));
        event.event_type = "response.custom_tool_call_input.done";
        event
            .payload
            .insert("input".into(), Value::String(input.into()));
        return Ok(vec![delta, event]);
    }
    if let Some(Value::String(name)) = event.payload.get_mut("name")
        && let Some(tool) = names.get(name)
    {
        *name = tool.name.clone();
    }
    Ok(vec![event])
}
