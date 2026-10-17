// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Target transport; the bound Target owns every request round.

use std::pin::Pin;

use futures::{Stream, StreamExt};
use serde::Deserialize;

use crate::LlmFacadeError;
use crate::http::{
    classify_reqwest, classify_response_failure, classify_status, is_ndjson, ndjson_lines,
    validate_endpoint,
};

pub use foretoken_model_protocol::{
    DraftTargetRequest, DraftTargetSampling, RoleStatus, TargetCommit, TargetStopReason,
};

#[derive(Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
enum RoleEvent {
    AdmissionClosed,
    Opened { session_id: String },
    Committed(Box<TargetCommit>),
}

type RoleStream = Pin<Box<dyn Stream<Item = Result<RoleEvent, LlmFacadeError>> + Send>>;

/// HTTP adapter for one selected role endpoint. It does not choose peers or order rounds.
#[derive(Clone)]
pub struct RoleClient {
    endpoint: String,
    client: reqwest::Client,
}

impl RoleClient {
    /// Creates a transport for an already selected role; invalid URLs fail before admission.
    pub fn new(endpoint: String) -> Result<Self, LlmFacadeError> {
        Ok(Self {
            endpoint: validate_endpoint(endpoint)?,
            // Role ASGI servers close idle HTTP/1 connections after five seconds.
            // Retire pooled controls first; a stale POST connection cannot be replayed.
            client: reqwest::Client::builder()
                .pool_idle_timeout(std::time::Duration::from_secs(1))
                .build()
                .map_err(role_transport_error)?,
        })
    }

    /// Identifies a routed role when encoding the fixed request binding.
    fn endpoint(&self) -> &str {
        &self.endpoint
    }

    /// Reads live role capabilities for the frontend's binding decision.
    pub async fn status(&self) -> Result<RoleStatus, LlmFacadeError> {
        let response = self
            .client
            .get(format!("{}/status", self.endpoint))
            .send()
            .await
            .map_err(role_transport_error)?;
        if !response.status().is_success() {
            return Err(classify_status(response.status()));
        }
        response.json().await.map_err(|_| LlmFacadeError::Protocol)
    }

    /// Opens Target prefill with a fixed Router binding; Target owns all subsequent rounds.
    pub async fn generate_with_drafts(
        &self,
        request: &DraftTargetRequest,
        request_id: &str,
        drafts: &[RoleClient],
    ) -> Result<Option<RoleSession>, LlmFacadeError> {
        let mut body = serde_json::to_value(request).map_err(|_| LlmFacadeError::Configuration)?;
        body["request_id"] = serde_json::json!(request_id);
        body["target_endpoint"] = serde_json::json!(self.endpoint);
        body["draft_endpoints"] =
            serde_json::json!(drafts.iter().map(RoleClient::endpoint).collect::<Vec<_>>());
        self.open("generate", &body).await
    }

    async fn open(
        &self,
        path: &str,
        body: &serde_json::Value,
    ) -> Result<Option<RoleSession>, LlmFacadeError> {
        let response = self
            .client
            .post(format!("{}/{path}", self.endpoint))
            .json(body)
            .send()
            .await
            .map_err(role_transport_error)?;
        if !response.status().is_success() {
            return Err(classify_response_failure(response).await);
        }
        RoleSession::open(response, self.clone()).await
    }

    /// Explicitly acknowledges session termination, native GPU fencing and artifact cleanup.
    async fn close_session(&self, session: &str) -> Result<(), LlmFacadeError> {
        let response = self
            .client
            .delete(format!("{}/sessions/{session}", self.endpoint))
            .send()
            .await
            .map_err(role_transport_error)?;
        if !response.status().is_success() {
            return Err(classify_status(response.status()));
        }
        Ok(())
    }
}

/// Connection-owned role session. Dropping it closes the body and cancels remote model work.
pub struct RoleSession {
    stream: RoleStream,
    id: String,
    client: RoleClient,
    closed: bool,
}

impl RoleSession {
    // Consumes the admission event while retaining the response body as the cancellation owner.
    async fn open(
        response: reqwest::Response,
        client: RoleClient,
    ) -> Result<Option<Self>, LlmFacadeError> {
        if !is_ndjson(response.headers().get(reqwest::header::CONTENT_TYPE)) {
            return Err(LlmFacadeError::Protocol);
        }
        let mut stream: RoleStream = Box::pin(async_stream::try_stream! {
            let mut lines = ndjson_lines(response);
            while let Some(line) = lines.next().await {
                yield serde_json::from_slice(&line?).map_err(|_| LlmFacadeError::Protocol)?;
            }
        });
        match stream.next().await.transpose()? {
            Some(RoleEvent::Opened { session_id }) => Ok(Some(Self {
                stream,
                id: session_id,
                client,
                closed: false,
            })),
            Some(RoleEvent::AdmissionClosed) => Ok(None),
            _ => Err(LlmFacadeError::Protocol),
        }
    }

    /// Joins explicit remote cleanup before releasing this control stream.
    pub async fn close(mut self) -> Result<(), LlmFacadeError> {
        self.client.close_session(&self.id).await?;
        self.closed = true;
        Ok(())
    }

    /// Reads one Target commit; an early EOF cannot masquerade as successful generation.
    pub async fn next_commit(&mut self) -> Result<TargetCommit, LlmFacadeError> {
        match self.stream.next().await.transpose()? {
            Some(RoleEvent::Committed(commit)) => Ok(*commit),
            _ => Err(LlmFacadeError::Protocol),
        }
    }
}

impl Drop for RoleSession {
    fn drop(&mut self) {
        if !self.closed
            && let Ok(runtime) = tokio::runtime::Handle::try_current()
        {
            let client = self.client.clone();
            let id = self.id.clone();
            runtime.spawn(async move {
                if let Err(error) = client.close_session(&id).await {
                    tracing::error!(target: "foretoken::dt", session_id = id, %error, "DT session cleanup failed");
                }
            });
        }
    }
}

/// Preserve role transport diagnostics while exposing only classified errors to public callers.
fn role_transport_error(error: reqwest::Error) -> LlmFacadeError {
    tracing::error!(target: "foretoken::dt", error = ?error, "role control transport failed");
    classify_reqwest(error)
}
