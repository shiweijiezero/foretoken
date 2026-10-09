// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Normalizes admission facts without rendering, tokenizing, or retaining input payloads.

use std::time::Instant;

use foretoken_admission::{
    AdmissionApi, AdmissionInput, AdmissionInputKind, AdmissionMedia, AdmissionOperation,
    AdmissionOutput, AdmissionRequest, AdmissionTokenCount,
};
use foretoken_chat::{
    AssistantContentBlock, ChatContent, ChatContentPart, ChatMessage, ChatRequest,
};
use foretoken_text::Prompt;

use crate::runtime::GenerationRequest;

/// HTTP origin retained by CPU-only endpoints across request conversion and admission.
#[derive(Clone, Copy, Debug)]
pub struct AdmissionOrigin {
    pub api: Option<AdmissionApi>,
    pub received_at: Instant,
}

/// Summarizes one completion input for admission without encoding text or copying token IDs.
pub(crate) fn prompt_input(prompt: &Prompt) -> AdmissionInput {
    let (kind, text_bytes, tokens) = match prompt {
        Prompt::Text(text) => (
            AdmissionInputKind::Text,
            Some(text.len()),
            AdmissionTokenCount::Unknown,
        ),
        Prompt::TokenIds(tokens) => (
            AdmissionInputKind::TokenIds,
            None,
            AdmissionTokenCount::Exact(tokens.len() as u64),
        ),
    };
    AdmissionInput {
        kind,
        text_bytes,
        tokens,
        messages: None,
        media: AdmissionMedia::default(),
    }
}

/// Summarizes chat history for admission, including reasoning and tool-call argument text.
/// Tool declarations, templates, and encoded media are excluded; total token cost remains unknown.
pub(crate) fn chat_input(chat: &ChatRequest) -> AdmissionInput {
    let mut text_bytes = 0;
    let mut media = AdmissionMedia::default();
    for message in &chat.messages {
        match message {
            ChatMessage::System { content }
            | ChatMessage::Developer { content, .. }
            | ChatMessage::User { content }
            | ChatMessage::ToolResponse { content, .. } => {
                summarize_content(content, &mut text_bytes, &mut media);
            }
            ChatMessage::Assistant { content } => {
                for block in content {
                    text_bytes += match block {
                        AssistantContentBlock::Text { text }
                        | AssistantContentBlock::Reasoning { text } => text.len(),
                        AssistantContentBlock::ToolCall(call) => call.arguments.len(),
                    };
                }
            }
        }
    }
    AdmissionInput {
        kind: AdmissionInputKind::Chat,
        text_bytes: Some(text_bytes),
        tokens: AdmissionTokenCount::Unknown,
        messages: Some(chat.messages.len()),
        media,
    }
}

/// Counts textual bytes and media items in one message without decoding or copying its content.
fn summarize_content(content: &ChatContent, text_bytes: &mut usize, media: &mut AdmissionMedia) {
    match content {
        ChatContent::Text(text) => *text_bytes += text.len(),
        ChatContent::Parts(parts) => {
            for part in parts {
                match part {
                    ChatContentPart::Text { text } => *text_bytes += text.len(),
                    ChatContentPart::ImageUrl { .. } => media.images += 1,
                    ChatContentPart::VideoUrl { .. } => media.video += 1,
                    ChatContentPart::InputAudio { .. } | ChatContentPart::AudioUrl { .. } => {
                        media.audio += 1;
                    }
                }
            }
        }
    }
}

/// Builds one candidate's admission facts before generation preprocessing starts.
/// Requested and execution output limits remain distinct; no output cost is predicted.
pub(crate) fn generation(
    request: &GenerationRequest,
    chat: Option<&ChatRequest>,
) -> AdmissionRequest {
    let (operation, input) = match chat {
        Some(chat) => (AdmissionOperation::Chat, chat_input(chat)),
        None => (
            AdmissionOperation::Completion,
            prompt_input(&request.prompt),
        ),
    };
    AdmissionRequest {
        model: request.model.clone(),
        operation,
        api: request.api,
        request_id: Some(request.request_id.clone()),
        inputs: vec![input],
        candidates_per_input: 1,
        output: AdmissionOutput {
            requested_max_tokens: request.requested_max_tokens,
            execution_max_tokens: request.sampling_params.max_tokens,
            expected_tokens: None,
        },
        requested_priority: request.priority,
        stream: request.intermediate,
        received_at: request.started_at,
    }
}

/// Builds prompt-tokenization admission facts using the CPU endpoint's original HTTP timing.
pub(crate) fn tokenize(model: &str, prompt: &Prompt, origin: AdmissionOrigin) -> AdmissionRequest {
    preprocessing(
        model,
        AdmissionOperation::Tokenization,
        prompt_input(prompt),
        origin,
    )
}

/// Builds chat-tokenization admission facts without treating renderer limits as output budgets.
pub(crate) fn tokenize_chat(
    model: &str,
    chat: &ChatRequest,
    origin: AdmissionOrigin,
) -> AdmissionRequest {
    let mut request = preprocessing(
        model,
        AdmissionOperation::Tokenization,
        chat_input(chat),
        origin,
    );
    request.request_id = Some(chat.request_id.clone());
    request.requested_priority = chat.priority;
    request
}

/// Builds detokenization admission facts from the supplied token count without retaining IDs.
pub(crate) fn detokenize(
    model: &str,
    token_count: usize,
    origin: AdmissionOrigin,
) -> AdmissionRequest {
    preprocessing(
        model,
        AdmissionOperation::Detokenization,
        AdmissionInput {
            kind: AdmissionInputKind::TokenIds,
            text_bytes: None,
            tokens: AdmissionTokenCount::Exact(token_count as u64),
            messages: None,
            media: AdmissionMedia::default(),
        },
        origin,
    )
}

/// Gives CPU-only operations one work unit and no output-generation budget or prediction.
fn preprocessing(
    model: &str,
    operation: AdmissionOperation,
    input: AdmissionInput,
    origin: AdmissionOrigin,
) -> AdmissionRequest {
    AdmissionRequest {
        model: model.to_owned(),
        operation,
        api: origin.api,
        request_id: None,
        inputs: vec![input],
        candidates_per_input: 1,
        output: AdmissionOutput::default(),
        requested_priority: 0,
        stream: false,
        received_at: origin.received_at,
    }
}
