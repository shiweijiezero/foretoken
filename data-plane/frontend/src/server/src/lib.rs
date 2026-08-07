// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Thin OpenAI-compatible HTTP adapters over Foretoken's vLLM output stream.

mod http;
mod response;
mod runtime;

pub use http::router;
pub use response::token_stream;
pub use runtime::{
    Generated, GeneratedChat, Generation, GenerationError, GenerationRequest, KvIndexDiagnostics,
    ModelRuntime, RoutedGenerate, RoutedRequest, RuntimeBundle, RuntimeControl, RuntimeDiagnostics,
    RuntimeGeneration, RuntimeState, Tokenization,
};

#[cfg(test)]
pub(crate) use response::{
    CompletionResponseOptions, chat_collected, idle_timed, stream_response, text_collected,
    text_collected_many, text_stream, text_stream_with_options,
};

#[cfg(test)]
#[path = "tests/http_api.rs"]
mod tests;
