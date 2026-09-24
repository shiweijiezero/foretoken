// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Responses errors at the HTTP boundary; backend details remain private.

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde_json::{Value, json};

use crate::runtime::GenerationError;

/// A protocol error returned before generation or represented in a failed response.
pub(super) struct ApiError {
    pub status: StatusCode,
    pub body: Value,
}

impl ApiError {
    /// Reject a client input without starting an inference request.
    pub fn invalid_request(message: impl Into<String>, param: Option<&str>) -> Self {
        Self {
            status: StatusCode::BAD_REQUEST,
            body: json!({"message": message.into(), "type": "invalid_request_error", "param": param, "code": "invalid_request_error"}),
        }
    }

    /// Map execution failures without exposing internal addresses or backend diagnostics.
    pub fn generation(error: GenerationError) -> Self {
        let (status, code) = match error {
            GenerationError::InvalidRequest => (StatusCode::BAD_REQUEST, "invalid_request_error"),
            GenerationError::ModelNotFound => (StatusCode::NOT_FOUND, "model_not_found"),
            GenerationError::Unavailable => {
                (StatusCode::SERVICE_UNAVAILABLE, "service_unavailable")
            }
            GenerationError::DeadlineExceeded => (StatusCode::GATEWAY_TIMEOUT, "request_timeout"),
            GenerationError::BackendRejected => (StatusCode::BAD_GATEWAY, "backend_rejected"),
            GenerationError::BackendProtocol | GenerationError::RequestFailed => {
                (StatusCode::BAD_GATEWAY, "generation_failed")
            }
            GenerationError::Internal => (StatusCode::INTERNAL_SERVER_ERROR, "internal_error"),
        };
        Self {
            status,
            body: json!({"message": error.to_string(), "type": code, "param": null, "code": code}),
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.status, Json(json!({"error": self.body}))).into_response()
    }
}

// Kept local to the adapted upstream converter, rather than exporting a second error API.
macro_rules! bail_invalid_request {
    (param = $param:expr, $($message:tt)*) => {
        return Err(super::error::ApiError::invalid_request(format!($($message)*), Some($param)))
    };
}
pub(super) use bail_invalid_request;
