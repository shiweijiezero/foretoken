// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Thin OpenAI-compatible HTTP adapters over Foretoken's vLLM output stream.

mod http;
mod response;
mod runtime;

pub use http::router;
pub use runtime::{
    Generated, GeneratedChat, Generation, GenerationError, GenerationRequest, KvIndexDiagnostics,
    ModelRuntime, RoutedGenerate, RoutedRequest, RuntimeBundle, RuntimeControl, RuntimeDiagnostics,
    RuntimeGeneration, RuntimeState, Tokenization,
};
