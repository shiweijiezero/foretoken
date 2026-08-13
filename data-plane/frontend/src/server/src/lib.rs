// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! OpenAI-compatible HTTP frontend for the Foretoken data plane.
//!
//! A thin HTTP shell: it parses OpenAI requests, routes them, and streams
//! responses back. It owns no routing, tokenization, or inference logic.

mod config;
mod dto;
mod error;
mod handler;
mod mock;
mod routes;
mod server;

pub use config::ServerConfig;
pub use dto::{ChatCompletion, ChatCompletionChunk, ChatCompletionRequest, ChatMessage, ChatRole};
pub use error::{ApiError, ErrorResponse};
pub use routes::build_router;
pub use server::{serve, serve_listener};
