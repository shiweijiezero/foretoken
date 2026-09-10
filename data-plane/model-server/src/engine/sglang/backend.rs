// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! SGLang adapter using the loopback HTTP API.

use async_trait::async_trait;
use bytes::Bytes;
use futures::{Stream, StreamExt};
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::engine::{Engine, EngineError, EngineTelemetry, TokenStream};
use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo};

use super::conversion::{SglangRequest, SglangResponseDecoder, parse_sse_chunk};

#[derive(serde::Serialize)]
struct SglangAbortRequest<'a> {
    rid: &'a str,
    abort_all: bool,
}

/// HTTP-backed SGLang engine.
pub struct SglangBackend {
    client: reqwest::Client,
    endpoint: String,
    running_requests: Arc<AtomicU64>,
}

impl SglangBackend {
    pub fn new(endpoint: String) -> Self {
        Self {
            client: reqwest::Client::new(),
            endpoint: endpoint.trim_end_matches('/').to_owned(),
            running_requests: Arc::new(AtomicU64::new(0)),
        }
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.endpoint)
    }

    /// Converts SGLang's streaming response into the engine-neutral token stream.
    fn token_stream(
        request_id: String,
        prompt_token_ids: Vec<u32>,
        body: impl Stream<Item = Result<Bytes, reqwest::Error>> + Unpin + Send + 'static,
    ) -> TokenStream {
        let stream = async_stream::stream! {
            let mut body = body;
            let mut pending: Vec<u8> = Vec::new();
            let mut first_output = true;
            let mut decoder = SglangResponseDecoder::default();
            while let Some(chunk) = body.next().await {
                let chunk = match chunk {
                    Ok(chunk) => chunk,
                    Err(_) => {
                        yield Err(EngineError::Protocol);
                        return;
                    }
                };
                pending.extend_from_slice(&chunk);
                while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
                    let line: Vec<u8> = pending.drain(..=newline).collect();
                    let chunk = match parse_sse_chunk(&line[..line.len() - 1]) {
                        Ok(Some(chunk)) => chunk,
                        Ok(None) => continue,
                        Err(()) => {
                            yield Err(EngineError::Protocol);
                            return;
                        }
                    };
                    let step = decoder.decode(chunk);
                    if step.finish_reason == Some(FinishReason::Error) {
                        yield Err(EngineError::RequestFailed);
                        return;
                    }
                    yield Ok(GenerateOutput {
                        request_id: request_id.clone(),
                        prompt_info: if first_output {
                            Some(GeneratePromptInfo {
                                prompt_token_ids: prompt_token_ids.clone().into(),
                                prompt_logprobs: None,
                            })
                        } else {
                            None
                        },
                        token_ids: step.token_ids,
                        logprobs: step.logprobs,
                        finish_reason: step.finish_reason,
                        cached_token_count: 0,
                        kv_transfer_params: None,
                        ec_transfer_params: None,
                    });
                    first_output = false;
                }
            }
            if !pending.is_empty() {
                yield Err(EngineError::Protocol);
            }
        };
        Box::pin(stream)
    }
}

#[async_trait]
impl Engine for SglangBackend {
    async fn generate(
        &self,
        request: vllm_llm::GenerateRequest,
    ) -> Result<TokenStream, EngineError> {
        let body = SglangRequest::try_from(&request).map_err(|field| {
            tracing::warn!(field, "rejecting field");
            EngineError::InvalidRequest
        })?;
        let request_id = request.request_id.clone();
        let prompt_token_ids = request.prompt_token_ids.clone();

        let response = self
            .client
            .post(self.url("/generate"))
            .json(&body)
            .send()
            .await
            .map_err(|error| {
                if error.is_connect() || error.is_timeout() {
                    EngineError::Unavailable
                } else {
                    EngineError::RequestFailed
                }
            })?;
        if !response.status().is_success() {
            return Err(if response.status().is_server_error() {
                EngineError::Unavailable
            } else {
                EngineError::InvalidRequest
            });
        }

        let running_requests = self.running_requests.clone();
        running_requests.fetch_add(1, Ordering::AcqRel);
        let guard = RunningGuard { running_requests };
        let stream = Self::token_stream(request_id, prompt_token_ids, response.bytes_stream());
        let stream = stream.scan(guard, |_guard, event| async move { Some(event) });

        Ok(Box::pin(stream))
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), EngineError> {
        for request_id in request_ids {
            let response = self
                .client
                .post(self.url("/abort_request"))
                .json(&SglangAbortRequest {
                    rid: request_id,
                    abort_all: false,
                })
                .send()
                .await
                .map_err(|error| {
                    if error.is_connect() || error.is_timeout() {
                        EngineError::Unavailable
                    } else {
                        EngineError::RequestFailed
                    }
                })?;
            if !response.status().is_success() {
                return Err(if response.status().is_server_error() {
                    EngineError::Unavailable
                } else {
                    EngineError::RequestFailed
                });
            }
        }
        Ok(())
    }

    fn telemetry(&self) -> EngineTelemetry {
        EngineTelemetry {
            running_requests: self.running_requests.load(Ordering::Acquire),
            ..Default::default()
        }
    }

    async fn cleanup(&self) -> Result<(), EngineError> {
        Ok(())
    }
}

/// Decrements the running-request counter when a stream completes or is dropped.
struct RunningGuard {
    running_requests: Arc<AtomicU64>,
}

impl Drop for RunningGuard {
    fn drop(&mut self) {
        self.running_requests.fetch_sub(1, Ordering::AcqRel);
    }
}
