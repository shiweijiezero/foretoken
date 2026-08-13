// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! OpenAI-compatible HTTP frontend for the Foretoken data plane.
//!
//! A thin HTTP shell: it parses OpenAI requests, renders + tokenizes them via
//! the chat facade, forwards them to a model-server, and streams tokens back.
//! It owns no routing or inference logic.

mod config;
mod dto;
mod error;
mod handler;
mod routes;
mod server;
mod state;

pub use config::ServerConfig;
pub use dto::{ChatCompletion, ChatCompletionChunk, ChatCompletionRequest, ChatMessage, ChatRole};
pub use error::{ApiError, ErrorResponse};
pub use routes::build_router;
pub use server::{serve, serve_listener};
pub use state::AppState;
