// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//! HTTP model-server transport, NDJSON decoding, and endpoint helpers.

use std::collections::BTreeMap;
use std::time::Duration;

use foretoken_model_protocol::{AbortInput, GenerateInput, TokenErrorCode, TokenEvent};
use futures::StreamExt;
use serde::Deserialize;
use vllm_llm::{FinishReason, GenerateOutput, GenerateRequest};

use crate::{LlmFacade, LlmFacadeError, TokenStream};

pub(crate) const MODEL_SERVER_REQUEST_START_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Clone)]
pub struct HttpFacade {
    endpoint: String,
    client: reqwest::Client,
    request_start_timeout: Duration,
}
impl HttpFacade {
    pub fn new(endpoint: String) -> Result<Self, LlmFacadeError> {
        Self::with_request_start_timeout(endpoint, MODEL_SERVER_REQUEST_START_TIMEOUT)
    }
    fn with_request_start_timeout(
        endpoint: String,
        request_start_timeout: Duration,
    ) -> Result<Self, LlmFacadeError> {
        Ok(Self {
            endpoint: validate_endpoint(endpoint)?,
            client: reqwest::Client::new(),
            request_start_timeout,
        })
    }
    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.endpoint)
    }

    pub(crate) async fn generate(
        &self,
        request: GenerateRequest,
    ) -> Result<TokenStream, LlmFacadeError> {
        let response = tokio::time::timeout(
            self.request_start_timeout,
            self.client
                .post(self.url("/v1/internal/generate"))
                .json(&GenerateInput::from(request))
                .send(),
        )
        .await
        .map_err(|_| LlmFacadeError::Unavailable)?
        .map_err(classify_reqwest)?;
        if !response.status().is_success() {
            return Err(classify_status(response.status()));
        }
        if !is_ndjson(response.headers().get(reqwest::header::CONTENT_TYPE)) {
            return Err(LlmFacadeError::Protocol);
        }
        Ok(Box::pin(async_stream::stream! {
            let mut body = Box::pin(response.bytes_stream()); let mut pending = Vec::new();
            while let Some(chunk) = body.next().await {
                match chunk { Ok(chunk) => pending.extend_from_slice(&chunk), Err(error) => { yield Err(classify_reqwest(error)); return; } }
                while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
                    let line: Vec<_> = pending.drain(..=newline).collect();
                    match decode_event(&line[..line.len() - 1]) { Ok(output) => yield Ok(output), Err(error) => { yield Err(error); return; } }
                }
            }
            if !pending.is_empty() { yield decode_event(&pending); }
        }))
    }
    pub(crate) async fn abort(&self, request_ids: &[String]) -> Result<(), LlmFacadeError> {
        let response = tokio::time::timeout(
            self.request_start_timeout,
            self.client
                .post(self.url("/v1/internal/abort"))
                .json(&AbortInput {
                    request_ids: request_ids.to_vec(),
                })
                .send(),
        )
        .await
        .map_err(|_| LlmFacadeError::Unavailable)?
        .map_err(classify_reqwest)?;
        if response.status().is_success() {
            Ok(())
        } else {
            Err(classify_status(response.status()))
        }
    }
}

#[async_trait::async_trait]
impl LlmFacade for HttpFacade {
    async fn generate(&self, request: GenerateRequest) -> Result<TokenStream, LlmFacadeError> {
        self.generate(request).await
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), LlmFacadeError> {
        self.abort(request_ids).await
    }
}

pub(crate) fn validate_endpoint(endpoint: String) -> Result<String, LlmFacadeError> {
    let endpoint = endpoint.trim_end_matches('/').to_owned();
    let url = reqwest::Url::parse(&endpoint).map_err(|_| LlmFacadeError::Configuration)?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err(LlmFacadeError::Configuration);
    }
    Ok(endpoint)
}
fn classify_reqwest(error: reqwest::Error) -> LlmFacadeError {
    if error.is_timeout() || error.is_connect() {
        LlmFacadeError::Unavailable
    } else {
        LlmFacadeError::RequestFailed
    }
}
fn classify_status(status: reqwest::StatusCode) -> LlmFacadeError {
    if status == reqwest::StatusCode::SERVICE_UNAVAILABLE || status.is_server_error() {
        LlmFacadeError::Unavailable
    } else if status.is_client_error() {
        LlmFacadeError::Rejected
    } else {
        LlmFacadeError::Protocol
    }
}
fn is_ndjson(value: Option<&reqwest::header::HeaderValue>) -> bool {
    value.and_then(|v| v.to_str().ok()).is_some_and(|v| {
        v.split(';')
            .next()
            .is_some_and(|media| media.trim().eq_ignore_ascii_case("application/x-ndjson"))
    })
}

#[derive(Deserialize)]
struct BootstrapRank {
    engine_id: String,
}
/// Fetches the rank-zero prefill engine ID required by Mooncake P/D dispatch.
pub async fn bootstrap_engine_id(
    client: &reqwest::Client,
    bootstrap_endpoint: &str,
) -> Result<String, LlmFacadeError> {
    let response = client
        .get(format!(
            "{}/query",
            bootstrap_endpoint.trim_end_matches('/')
        ))
        .send()
        .await
        .map_err(classify_reqwest)?;
    if !response.status().is_success() {
        return Err(classify_status(response.status()));
    }
    let ranks: BTreeMap<String, BootstrapRank> = response
        .json()
        .await
        .map_err(|_| LlmFacadeError::Protocol)?;
    ranks
        .get("0")
        .filter(|rank| !rank.engine_id.is_empty())
        .map(|rank| rank.engine_id.clone())
        .ok_or(LlmFacadeError::Protocol)
}

fn decode_event(line: &[u8]) -> Result<GenerateOutput, LlmFacadeError> {
    match serde_json::from_slice(line).map_err(|_| LlmFacadeError::Protocol)? {
        TokenEvent::Token(output) if output.finish_reason == Some(FinishReason::Error) => {
            Err(LlmFacadeError::RequestFailed)
        }
        TokenEvent::Token(output) => Ok((*output).into()),
        TokenEvent::Error { code, .. } => Err(match code {
            TokenErrorCode::Unavailable => LlmFacadeError::Unavailable,
            TokenErrorCode::RequestFailed => LlmFacadeError::RequestFailed,
            TokenErrorCode::Protocol => LlmFacadeError::Protocol,
        }),
    }
}
