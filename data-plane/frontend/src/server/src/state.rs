// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared request-handling state.

use std::sync::Arc;

use foretoken_chat::ChatFacade;

/// State shared across all requests, assembled once at startup.
#[derive(Clone)]
pub struct AppState {
    pub chat: Arc<ChatFacade>,
    pub model_server_url: String,
}
