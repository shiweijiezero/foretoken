// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! HTTP route assembly.

use axum::Router;
use axum::routing::{get, post};

use crate::handler;

/// Build the frontend router: chat completions + health.
pub fn build_router() -> Router {
    Router::new()
        .route("/v1/chat/completions", post(handler::chat_completions))
        .route("/health", get(handler::health))
}
