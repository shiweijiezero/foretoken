// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared decoding, idle deadlines, and SSE transport for inference APIs.

use std::convert::Infallible;
use std::time::Duration;

use axum::response::sse::{Event, KeepAlive, Sse};
use axum::response::{IntoResponse, Response};
use foretoken_chat::ChatEvent;
use foretoken_text::output::decoded_text_event_stream;
use futures::{Stream, StreamExt};

use crate::runtime::{Generated, GeneratedChat};

/// Builds protocol SSE responses with transport heartbeats during prefill or hidden reasoning.
///
/// Heartbeats keep client connections alive without resetting backend idle or request deadlines.
pub(crate) fn sse_response(
    events: impl Stream<Item = Result<Event, Infallible>> + Send + 'static,
) -> Response {
    Sse::new(events)
        .keep_alive(KeepAlive::default())
        .into_response()
}

/// Decodes backend tokens while retaining ownership of the cancellable output stream.
pub(super) fn decoded(generated: Generated) -> impl foretoken_text::TextOutputStream {
    let request_id = generated.routed.routed_request.request.request_id.clone();
    let mut stream = generated.routed.stream;
    let raw = async_stream::stream! {
        while let Some(item) = stream.next().await {
            match item {
                Ok(output) => yield Ok(output),
                Err(_) => break,
            }
        }
    };
    decoded_text_event_stream(
        request_id,
        generated.tokenizer,
        Box::pin(raw),
        generated.decode_options,
        true,
    )
}

/// Applies the HTTP stream-idle budget to decoded backend output.
///
/// Streaming and collected response adapters wrap their output with this function. It returns a
/// stream that preserves decoded events until the idle budget expires, then yields one terminal
/// error so dropping the wrapper cancels the underlying backend request.
pub(crate) fn idle_timed(
    stream: impl foretoken_text::TextOutputStream,
    idle: Duration,
) -> impl foretoken_text::TextOutputStream {
    async_stream::stream! {
        let mut stream = Box::pin(stream);
        loop {
            match tokio::time::timeout(idle, stream.next()).await {
                Ok(Some(event)) => yield event,
                Ok(None) => break,
                Err(_) => {
                    // Cancel before yielding: the HTTP reader may stop polling after the error.
                    drop(stream);
                    yield Err(foretoken_text::Error::StreamClosedBeforeTerminalOutput {
                        request_id: "idle-timeout".into(),
                    });
                    break;
                }
            }
        }
    }
}

/// Parses the shared decoded stream into structured chat events for every HTTP protocol.
///
/// The runtime stream retains the total deadline and backend cleanup; this layer adds the
/// independently configured idle budget before the model-specific output processor runs.
pub(crate) fn chat_events(
    generated: GeneratedChat,
    idle: Duration,
) -> foretoken_chat::Result<(
    bool,
    impl futures::Stream<Item = foretoken_chat::Result<ChatEvent>> + Send,
)> {
    let GeneratedChat {
        generated,
        output_processor,
        include_reasoning,
    } = generated;
    let decoded = idle_timed(decoded(generated), idle)
        .map(|event| event.map_err(foretoken_chat::Error::from));
    output_processor
        .process(Box::pin(decoded))
        .map(|stream| (include_reasoning, stream))
}
