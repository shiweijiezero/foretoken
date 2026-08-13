// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! API errors mapped to OpenAI-style error responses.

use axum::Json;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use serde::Serialize;

/// OpenAI-style error body.
#[derive(Debug, Serialize)]
pub struct ErrorResponse {
    pub error: ErrorBody,
}

#[derive(Debug, Serialize)]
pub struct ErrorBody {
    pub message: String,
    #[serde(rename = "type")]
    pub kind: String,
}

/// Errors the HTTP frontend surfaces to clients.
#[derive(Debug, thiserror::Error)]
pub enum ApiError {
    #[error("bad request: {0}")]
    BadRequest(String),
    #[error("model not found: {0}")]
    ModelNotFound(String),
    #[error("internal error: {0}")]
    Internal(String),
}

impl ApiError {
    fn status(&self) -> StatusCode {
        match self {
            ApiError::BadRequest(_) => StatusCode::BAD_REQUEST,
            ApiError::ModelNotFound(_) => StatusCode::NOT_FOUND,
            ApiError::Internal(_) => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    fn kind(&self) -> &'static str {
        match self {
            ApiError::BadRequest(_) => "invalid_request_error",
            ApiError::ModelNotFound(_) => "model_not_found",
            ApiError::Internal(_) => "server_error",
        }
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let status = self.status();
        let body = ErrorResponse {
            error: ErrorBody {
                message: self.to_string(),
                kind: self.kind().to_string(),
            },
        };
        (status, Json(body)).into_response()
    }
}
