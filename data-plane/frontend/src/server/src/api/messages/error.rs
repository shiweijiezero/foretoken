// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Anthropic error mapping adapted from vLLM PR #52896's error.rs; Foretoken
//! generation errors remain the shared execution boundary.

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};

use crate::runtime::GenerationError;

/// A protocol failure returned before headers or encoded as an SSE error event.
pub(super) struct AnthropicApiError {
    pub status: StatusCode,
    pub kind: &'static str,
    pub message: String,
}

impl AnthropicApiError {
    /// Reports invalid or unsupported request semantics to a Messages caller.
    pub fn invalid(message: impl Into<String>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            kind: "invalid_request_error",
            message: message.into(),
        }
    }

    /// Hides backend diagnostics while terminating a failed output stream.
    pub fn stream() -> Self {
        Self {
            status: StatusCode::BAD_GATEWAY,
            kind: "api_error",
            message: "Model output could not be completed".into(),
        }
    }

    /// Serializes the same error envelope for JSON and named SSE events.
    pub fn body(&self) -> Value {
        json!({"type": "error", "error": {"type": self.kind, "message": self.message}})
    }
}

impl From<GenerationError> for AnthropicApiError {
    fn from(error: GenerationError) -> Self {
        let (status, kind) = match error {
            GenerationError::InvalidRequest | GenerationError::BackendRejected => {
                (StatusCode::BAD_REQUEST, "invalid_request_error")
            }
            GenerationError::ModelNotFound => (StatusCode::NOT_FOUND, "not_found_error"),
            GenerationError::Unavailable => (StatusCode::SERVICE_UNAVAILABLE, "overloaded_error"),
            GenerationError::DeadlineExceeded => (StatusCode::GATEWAY_TIMEOUT, "api_error"),
            GenerationError::BackendProtocol | GenerationError::RequestFailed => {
                (StatusCode::BAD_GATEWAY, "api_error")
            }
            GenerationError::Internal => (StatusCode::INTERNAL_SERVER_ERROR, "api_error"),
        };
        Self {
            status,
            kind,
            message: error.to_string(),
        }
    }
}

impl IntoResponse for AnthropicApiError {
    fn into_response(self) -> Response {
        (self.status, Json(self.body())).into_response()
    }
}
