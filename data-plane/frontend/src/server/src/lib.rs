// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! OpenAI and Anthropic HTTP adapters over Foretoken's shared generation pipeline.

mod admission;
mod api;

pub use admission::AdmissionOrigin;
mod http;
mod runtime;
mod video;
mod video_task;

pub use video::VideoRequest;

pub use http::router;
pub use runtime::{
    Generated, GeneratedChat, Generation, GenerationError, GenerationRequest, KvIndexDiagnostics,
    ModelRuntime, RoutedGenerate, RoutedRequest, RuntimeBundle, RuntimeControl, RuntimeDiagnostics,
    RuntimeGeneration, RuntimeState, Tokenization,
};
