// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! SGLang adapter backed by the engine's loopback HTTP `/generate` endpoint.

use async_trait::async_trait;
use futures::StreamExt;
use serde::Deserialize;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use crate::engine::{Engine, EngineCapabilities, EngineError, EngineTelemetry, TokenStream};
use foretoken_model_protocol::{
    FinishReason, GenerateInput, SamplingParams, TokenEvent, TokenOutput,
};

/// SGLang adapter failures, translated into the engine-neutral [`EngineError`].
#[derive(Debug, thiserror::Error, Clone, Copy, PartialEq, Eq)]
pub enum SglangError {
    #[error("request is invalid")]
    InvalidRequest,
    #[error("sglang is unavailable")]
    Unavailable,
    #[error("sglang protocol failed")]
    Protocol,
    #[error("sglang request failed")]
    RequestFailed,
}

impl From<SglangError> for EngineError {
    fn from(error: SglangError) -> Self {
        match error {
            SglangError::InvalidRequest => EngineError::InvalidRequest,
            SglangError::Unavailable => EngineError::Unavailable,
            SglangError::Protocol => EngineError::Protocol,
            SglangError::RequestFailed => EngineError::RequestFailed,
        }
    }
}

/// Request body for SGLang's native `/generate`.
#[derive(serde::Serialize)]
struct GenerateRequest {
    input_ids: Vec<u32>,
    sampling_params: serde_json::Value,
    stream: bool,
}

/// One streamed token chunk from SGLang.
#[derive(Debug, Deserialize)]
struct GenerateChunk {
    output_ids: Vec<u32>,
    #[serde(default)]
    meta_info: Option<ChunkMeta>,
}

#[derive(Debug, Deserialize)]
struct ChunkMeta {
    /// SGLang reports finish reasons as snake_case strings (`"stop"`,
    /// `"length"`, ...).
    finish_reason: Option<String>,
}

/// Maps an SGLang finish-reason string into the neutral [`FinishReason`].
fn parse_sglang_finish_reason(reason: &str) -> FinishReason {
    match reason {
        "stop" => FinishReason::Stop(None),
        "length" => FinishReason::Length,
        "abort" => FinishReason::Abort,
        "repetition" => FinishReason::Repetition(None),
        _ => FinishReason::Error,
    }
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

    fn sampling_json(params: &SamplingParams) -> serde_json::Value {
        let mut json = serde_json::json!({
            "temperature": params.temperature,
            "top_p": params.top_p,
            "top_k": params.top_k,
            "max_new_tokens": params.max_tokens,
            "frequency_penalty": params.frequency_penalty,
            "presence_penalty": params.presence_penalty,
        });
        if let Some(seed) = params.seed {
            json["seed"] = serde_json::json!(seed);
        }
        if !params.stop_token_ids.is_empty() {
            json["stop_token_ids"] = serde_json::json!(params.stop_token_ids);
        }
        // Forward engine-specific sampling overrides unchanged.
        for (key, value) in &params.extra_args {
            json[key] = value.clone();
        }
        json
    }
}

#[async_trait]
impl Engine for SglangBackend {
    async fn generate(&self, request: GenerateInput) -> Result<TokenStream, EngineError> {
        let request_id = request.request_id.clone();
        let prompt_token_ids = request.prompt_token_ids.clone();
        let sampling = Self::sampling_json(&request.sampling_params);
        let body = GenerateRequest {
            input_ids: prompt_token_ids,
            sampling_params: sampling,
            stream: true,
        };

        let response = self
            .client
            .post(self.url("/generate"))
            .json(&body)
            .send()
            .await
            .map_err(|error| {
                if error.is_connect() || error.is_timeout() {
                    SglangError::Unavailable
                } else {
                    SglangError::RequestFailed
                }
            })?;
        if !response.status().is_success() {
            return Err(if response.status().is_server_error() {
                SglangError::Unavailable
            } else {
                SglangError::InvalidRequest
            }
            .into());
        }

        let running_requests = self.running_requests.clone();
        let request_id_for_stream = request_id.clone();
        let stream = async_stream::stream! {
            let mut body = Box::pin(response.bytes_stream());
            let mut pending: Vec<u8> = Vec::new();
            while let Some(chunk) = body.next().await {
                let chunk = match chunk {
                    Ok(chunk) => chunk,
                    Err(_) => {
                        yield Err(EngineError::from(SglangError::Protocol));
                        return;
                    }
                };
                pending.extend_from_slice(&chunk);
                while let Some(newline) = pending.iter().position(|byte| *byte == b'\n') {
                    let line: Vec<u8> = pending.drain(..=newline).collect();
                    let line = &line[..line.len() - 1];
                    if line.is_empty() {
                        continue;
                    }
                    let chunk: GenerateChunk = match serde_json::from_slice(line) {
                        Ok(chunk) => chunk,
                        Err(_) => {
                            yield Err(EngineError::from(SglangError::Protocol));
                            return;
                        }
                    };
                    let finish_reason = chunk
                        .meta_info
                        .and_then(|meta| meta.finish_reason)
                        .map(|reason| parse_sglang_finish_reason(&reason))
                        .or_else(|| chunk.output_ids.is_empty().then_some(FinishReason::Length));
                    yield Ok(TokenEvent::Token(Box::new(TokenOutput {
                        request_id: request_id_for_stream.clone(),
                        prompt_token_ids: None,
                        prompt_logprobs: None,
                        token_ids: chunk.output_ids,
                        logprobs: None,
                        cached_token_count: 0,
                        finish_reason,
                        kv_transfer_params: None,
                        ec_transfer_params: None,
                    })));
                }
            }
            if !pending.is_empty() {
                yield Err(EngineError::from(SglangError::Protocol));
            }
        };

        // Track running requests for telemetry, decrement on terminal or drop.
        running_requests.fetch_add(1, Ordering::AcqRel);
        let guard = RunningGuard { running_requests };
        let stream = stream.scan(guard, |_guard, event| async move { Some(event) });

        Ok(Box::pin(stream))
    }

    async fn abort(&self, _request_ids: &[String]) -> Result<(), EngineError> {
        // SGLang does not expose a stable per-request abort endpoint for the
        // native `/generate` path; report success to keep the core contract.
        Ok(())
    }

    fn telemetry(&self) -> EngineTelemetry {
        EngineTelemetry {
            running_requests: self.running_requests.load(Ordering::Acquire),
            ..Default::default()
        }
    }

    fn capabilities(&self) -> EngineCapabilities {
        EngineCapabilities::default()
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
