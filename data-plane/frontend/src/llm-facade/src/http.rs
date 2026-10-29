// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! HTTP model-server transport, NDJSON decoding, and endpoint helpers.

use std::collections::BTreeMap;
use std::error::Error as _;
use std::io;
use std::sync::LazyLock;
use std::time::Duration;

use foretoken_model_protocol::{
    AbortInput, GenerateInput, KvReuseSource, TokenErrorCode, TokenEvent, TokenOutput,
};
use foretoken_request_ledger::ReservationRef;
use futures::StreamExt;
use serde::Deserialize;
use tokio_util::codec::{FramedRead, LinesCodec, LinesCodecError};
use tokio_util::io::StreamReader;
use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo, GenerateRequest};

use crate::{LlmFacade, LlmFacadeError, TokenStream};

pub(crate) const MODEL_SERVER_REQUEST_START_TIMEOUT: Duration = Duration::from_secs(10);

// Every workflow stage uses the same transport settings and shares its connection pool.
pub(crate) static MODEL_SERVER_CLIENT: LazyLock<reqwest::Client> =
    LazyLock::new(reqwest::Client::new);

#[derive(Clone)]
pub struct HttpFacade {
    endpoint: String,
    client: reqwest::Client,
    kv_reuse: Option<KvReuseSource>,
}
impl HttpFacade {
    /// Creates the transport resolved by the backend registry for one model-server endpoint.
    ///
    /// Returns a validated facade that lives with its registry entry, or a configuration error
    /// before that endpoint can enter a serving generation.
    pub fn new(endpoint: String) -> Result<Self, LlmFacadeError> {
        Ok(Self {
            endpoint: validate_endpoint(endpoint)?,
            client: MODEL_SERVER_CLIENT.clone(),
            kv_reuse: None,
        })
    }

    /// Attaches the selected Decode source to this request's Prefill transport.
    /// The receiving model server owns acquisition and native transfer cleanup.
    pub fn with_kv_reuse(mut self, source: KvReuseSource) -> Self {
        self.kv_reuse = Some(source);
        self
    }

    fn url(&self, path: &str) -> String {
        format!("{}{path}", self.endpoint)
    }

    /// Starts one model-server request and exposes its NDJSON output as the shared token stream.
    ///
    /// The `LlmFacade` adapter calls this for an admitted workflow stage. The returned stream owns
    /// incremental decoding until terminal output or cancellation; response-start failures return first.
    #[tracing::instrument(name = "model_server_request", level = "error", skip_all, fields(generation_id = %request.request_id))]
    pub(crate) async fn generate(
        &self,
        mut request: GenerateRequest,
        reservation: Option<ReservationRef>,
        role_priority: Option<i32>,
    ) -> Result<TokenStream, LlmFacadeError> {
        // Role priorities increase with urgency; vLLM orders smaller values first.
        // Complement reverses the complete i32 range without collapsing its extremes.
        // Without role rules, preserve the client's existing native priority convention.
        if let Some(priority) = role_priority {
            request.priority = !priority;
        }
        let mut input = GenerateInput::from(request);
        input.reservation = reservation;
        input.kv_reuse = self.kv_reuse.clone();
        let request_id = input.request_id.clone();
        let body = rmp_serde::to_vec_named(&input).map_err(|error| {
            tracing::error!(generation_id = %request_id, %error, "could not encode model-server request");
            LlmFacadeError::RequestFailed
        })?;
        let response = tokio::time::timeout(
            MODEL_SERVER_REQUEST_START_TIMEOUT,
            self.client
                .post(self.url("/v1/internal/generate"))
                .header(reqwest::header::CONTENT_TYPE, "application/msgpack")
                .body(body)
                .send(),
        )
        .await
        .map_err(|_| {
            tracing::warn!(generation_id = %request_id, "model-server response-start deadline elapsed");
            LlmFacadeError::Unavailable
        })?
        .map_err(|error| {
            let failure = classify_reqwest(&error);
            tracing::warn!(
                timeout = error.is_timeout(),
                connect = error.is_connect(),
                io_error_kind = ?io_error_kind(&error),
                error = %error.without_url(),
                generation_id = %request_id,
                "model-server generation request failed"
            );
            failure
        })?;
        if !response.status().is_success() {
            let status = response.status();
            if status == reqwest::StatusCode::SERVICE_UNAVAILABLE {
                let error = response.json::<serde_json::Value>().await;
                // These rejection codes are emitted before submission; generic 5xx are not replayable.
                if matches!(
                    error
                        .ok()
                        .as_ref()
                        .and_then(|body| body.get("error"))
                        .and_then(|error| error.get("code"))
                        .and_then(serde_json::Value::as_str),
                    Some("admission_busy" | "admission_unavailable")
                ) {
                    return Err(LlmFacadeError::Busy);
                }
            }
            if matches!(
                status,
                reqwest::StatusCode::BAD_REQUEST | reqwest::StatusCode::SERVICE_UNAVAILABLE
            ) {
                tracing::debug!(generation_id = %request_id, status = status.as_u16(), "model-server rejected generation");
            } else {
                tracing::warn!(generation_id = %request_id, status = status.as_u16(), "model-server rejected generation");
            }
            return Err(classify_status(status));
        }
        if !is_ndjson(response.headers().get(reqwest::header::CONTENT_TYPE)) {
            tracing::warn!(generation_id = %request_id, "model-server generation response is not NDJSON");
            return Err(LlmFacadeError::Protocol);
        }
        Ok(Box::pin(async_stream::stream! {
            let body = response.bytes_stream().map(|chunk| chunk.map_err(io::Error::other));
            let mut lines = FramedRead::new(StreamReader::new(body), LinesCodec::new());
            while let Some(line) = lines.next().await {
                let output = line.map_err(|error| classify_line_error(&request_id, error))
                    .and_then(|line| decode_event(&request_id, line.as_bytes()));
                let failed = output.is_err();
                yield output;
                if failed { return; }
            }
        }))
    }
    /// Asks this model server to cancel admitted work after its consumer ends early.
    ///
    /// Stream cleanup guards call this best-effort operation. A successful result ends the HTTP
    /// abort exchange; the backend retains responsibility for final request termination.
    #[tracing::instrument(name = "model_server_abort", level = "error", skip_all, fields(request_count = request_ids.len()))]
    pub(crate) async fn abort(&self, request_ids: &[String]) -> Result<(), LlmFacadeError> {
        let response = tokio::time::timeout(
            MODEL_SERVER_REQUEST_START_TIMEOUT,
            self.client
                .post(self.url("/v1/internal/abort"))
                .json(&AbortInput {
                    request_ids: request_ids.to_vec(),
                })
                .send(),
        )
        .await
        .map_err(|_| {
            tracing::warn!(?request_ids, "model-server abort deadline elapsed");
            LlmFacadeError::Unavailable
        })?
        .map_err(|error| {
            let failure = classify_reqwest(&error);
            tracing::warn!(
                timeout = error.is_timeout(),
                connect = error.is_connect(),
                io_error_kind = ?io_error_kind(&error),
                error = %error.without_url(),
                ?request_ids,
                "model-server abort request failed"
            );
            failure
        })?;
        if response.status().is_success() {
            Ok(())
        } else {
            tracing::debug!(
                status = response.status().as_u16(),
                "model-server rejected abort"
            );
            Err(classify_status(response.status()))
        }
    }
}

#[async_trait::async_trait]
impl LlmFacade for HttpFacade {
    async fn generate(
        &self,
        request: GenerateRequest,
        reservation: Option<ReservationRef>,
        role_priority: Option<i32>,
    ) -> Result<TokenStream, LlmFacadeError> {
        self.generate(request, reservation, role_priority).await
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), LlmFacadeError> {
        self.abort(request_ids).await
    }
}

/// Validates and normalizes a model-server base URL before facade construction.
///
/// [`HttpFacade::new`] consumes the returned trailing-slash-free endpoint for the facade lifetime;
/// invalid endpoints fail before the registry can publish a route to them.
pub(crate) fn validate_endpoint(endpoint: String) -> Result<String, LlmFacadeError> {
    let endpoint = endpoint.trim_end_matches('/').to_owned();
    let url = reqwest::Url::parse(&endpoint).map_err(|_| LlmFacadeError::Configuration)?;
    if !matches!(url.scheme(), "http" | "https") || url.host_str().is_none() {
        return Err(LlmFacadeError::Configuration);
    }
    Ok(endpoint)
}
/// Retains transport failure categories through the codec; malformed frames remain protocol errors.
fn classify_line_error(request_id: &str, error: LinesCodecError) -> LlmFacadeError {
    match error {
        LinesCodecError::Io(error) => {
            let error = error
                .into_inner()
                .and_then(|error| error.downcast::<reqwest::Error>().ok());
            match error {
                Some(error) => {
                    let failure = classify_reqwest(&error);
                    tracing::warn!(
                        generation_id = request_id,
                        io_error_kind = ?io_error_kind(&error),
                        error = %error.without_url(),
                        "model-server output transport failed"
                    );
                    failure
                }
                None => {
                    tracing::warn!(
                        generation_id = request_id,
                        "could not decode model-server output frame"
                    );
                    LlmFacadeError::Protocol
                }
            }
        }
        LinesCodecError::MaxLineLengthExceeded => {
            tracing::warn!(
                generation_id = request_id,
                "model-server output exceeded its frame limit"
            );
            LlmFacadeError::Protocol
        }
    }
}

/// Retains the operating-system failure category without logging an endpoint or request data.
fn io_error_kind(error: &reqwest::Error) -> Option<io::ErrorKind> {
    let mut source = error.source();
    while let Some(error) = source {
        if let Some(error) = error.downcast_ref::<io::Error>() {
            return Some(error.kind());
        }
        source = error.source();
    }
    None
}

fn classify_reqwest(error: &reqwest::Error) -> LlmFacadeError {
    if error.is_timeout() || error.is_connect() {
        LlmFacadeError::Unavailable
    } else {
        LlmFacadeError::RequestFailed
    }
}
fn classify_status(status: reqwest::StatusCode) -> LlmFacadeError {
    if status == reqwest::StatusCode::BAD_REQUEST {
        LlmFacadeError::InvalidRequest
    } else if status == reqwest::StatusCode::SERVICE_UNAVAILABLE || status.is_server_error() {
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
/// Fetches the selected prefill rank's engine ID for Mooncake P/D dispatch.
pub async fn bootstrap_engine_id(
    client: &reqwest::Client,
    bootstrap_endpoint: &str,
    data_parallel_rank: u32,
) -> Result<String, LlmFacadeError> {
    let response = client
        .get(format!(
            "{}/query",
            bootstrap_endpoint.trim_end_matches('/')
        ))
        .send()
        .await
        .map_err(|error| classify_reqwest(&error))?;
    if !response.status().is_success() {
        return Err(classify_status(response.status()));
    }
    let ranks: BTreeMap<String, BootstrapRank> = response
        .json()
        .await
        .map_err(|_| LlmFacadeError::Protocol)?;
    ranks
        .get(&data_parallel_rank.to_string())
        .filter(|rank| !rank.engine_id.is_empty())
        .map(|rank| rank.engine_id.clone())
        .ok_or(LlmFacadeError::Protocol)
}

fn decode_event(request_id: &str, line: &[u8]) -> Result<GenerateOutput, LlmFacadeError> {
    match serde_json::from_slice(line).map_err(|error| {
        tracing::warn!(
            generation_id = request_id,
            category = ?error.classify(),
            line = error.line(),
            column = error.column(),
            "model-server output event is invalid"
        );
        LlmFacadeError::Protocol
    })? {
        TokenEvent::Token(output) if output.finish_reason == Some(FinishReason::Error) => {
            Err(LlmFacadeError::RequestFailed)
        }
        TokenEvent::Token(output) => Ok(generate_output(*output)),
        TokenEvent::Error { code, .. } => Err(match code {
            TokenErrorCode::Unavailable => LlmFacadeError::Unavailable,
            TokenErrorCode::RequestFailed => LlmFacadeError::RequestFailed,
            TokenErrorCode::Protocol => LlmFacadeError::Protocol,
        }),
    }
}

fn generate_output(output: TokenOutput) -> GenerateOutput {
    GenerateOutput {
        request_id: output.request_id,
        prompt_info: output
            .prompt_token_ids
            .map(|prompt_token_ids| GeneratePromptInfo {
                prompt_token_ids: prompt_token_ids.into(),
                prompt_logprobs: output.prompt_logprobs,
            }),
        token_ids: output.token_ids,
        logprobs: output.logprobs,
        finish_reason: output.finish_reason,
        cached_token_count: output.cached_token_count,
        cache_write_token_count: output.cache_write_token_count,
        kv_transfer_params: output.kv_transfer_params,
        ec_transfer_params: output.ec_transfer_params,
    }
}
