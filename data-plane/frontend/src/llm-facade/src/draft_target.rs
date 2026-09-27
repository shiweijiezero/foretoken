// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Internal role transport. The frontend, not either model, orders drafting and verification.

use std::pin::Pin;

use futures::{Stream, StreamExt};
use serde::{Deserialize, Serialize, de::DeserializeOwned};

use crate::LlmFacadeError;
use crate::http::{classify_reqwest, classify_status, is_ndjson, ndjson_lines, validate_endpoint};

/// Token-input greedy request; the caller owns tokenization and model compatibility.
#[derive(Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DraftTargetRequest {
    pub token_ids: Vec<u32>,
    pub max_tokens: u32,
    #[serde(default)]
    pub stop: Vec<String>,
    #[serde(default)]
    pub stop_token_ids: Vec<u32>,
    #[serde(default)]
    pub min_tokens: u32,
    #[serde(default)]
    pub ignore_eos: bool,
}

/// Confirmed Target output and the next engine generation, never a Draft guess.
#[derive(Debug, Serialize, Deserialize)]
pub struct TargetCommit {
    pub token_ids: Vec<u32>,
    pub text: String,
    pub version: Option<u64>,
    pub finished: bool,
    pub finish_reason: Option<String>,
    pub stop_reason: Option<TargetStopReason>,
    pub cached_token_count: usize,
}

/// The exact reason Target stopped, retained for the frontend's output processor.
#[derive(Debug, Serialize, Deserialize)]
#[serde(untagged)]
pub enum TargetStopReason {
    TokenId(u32),
    Text(String),
}

#[derive(Deserialize)]
#[serde(tag = "event", rename_all = "snake_case")]
enum RoleEvent {
    Opened { session_id: String },
    Committed(TargetCommit),
}

type RoleStream = Pin<Box<dyn Stream<Item = Result<RoleEvent, LlmFacadeError>> + Send>>;

/// A role's current admission and candidate capabilities, queried before binding.
#[derive(Clone, Deserialize)]
pub struct RoleStatus {
    pub role: String,
    pub model: String,
    pub revision: Option<String>,
    pub tokenizer: String,
    pub tokenizer_revision: Option<String>,
    pub max_model_len: u32,
    pub accepting: bool,
    pub token_budget: u32,
    pub candidate_format: String,
}

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
            client: reqwest::Client::new(),
        })
    }

    async fn post(
        &self,
        path: &str,
        body: &impl Serialize,
    ) -> Result<reqwest::Response, LlmFacadeError> {
        let response = self
            .client
            .post(format!("{}{path}", self.endpoint))
            .json(body)
            .send()
            .await
            .map_err(classify_reqwest)?;
        if !response.status().is_success() {
            return Err(classify_status(response.status()));
        }
        Ok(response)
    }

    /// Reads live role capabilities for the frontend's binding decision.
    pub async fn status(&self) -> Result<RoleStatus, LlmFacadeError> {
        let response = self
            .client
            .get(format!("{}/status", self.endpoint))
            .send()
            .await
            .map_err(classify_reqwest)?;
        if !response.status().is_success() {
            return Err(classify_status(response.status()));
        }
        response.json().await.map_err(|_| LlmFacadeError::Protocol)
    }

    /// Opens Target generation; the returned session owns cancellation through its response body.
    pub async fn generate(
        &self,
        request: &DraftTargetRequest,
    ) -> Result<RoleSession, LlmFacadeError> {
        RoleSession::open(self.clone(), self.post("/generate", request).await?).await
    }

    /// Binds Draft to confirmed tokens and keeps the owning control response open.
    pub async fn bind(
        &self,
        token_ids: &[u32],
        version: u64,
    ) -> Result<RoleSession, LlmFacadeError> {
        RoleSession::open(
            self.clone(),
            self.post(
                "/sessions",
                &serde_json::json!({
                    "token_ids": token_ids, "version": version
                }),
            )
            .await?,
        )
        .await
    }
}

/// Connection-owned role session. Dropping it closes the body and cancels remote model work.
pub struct RoleSession {
    client: RoleClient,
    session_id: String,
    stream: RoleStream,
}

impl RoleSession {
    async fn open(client: RoleClient, response: reqwest::Response) -> Result<Self, LlmFacadeError> {
        if !is_ndjson(response.headers().get(reqwest::header::CONTENT_TYPE)) {
            return Err(LlmFacadeError::Protocol);
        }
        let mut stream: RoleStream = Box::pin(async_stream::try_stream! {
            let mut lines = ndjson_lines(response);
            while let Some(line) = lines.next().await {
                yield serde_json::from_slice(&line?).map_err(|_| LlmFacadeError::Protocol)?;
            }
        });
        let Some(RoleEvent::Opened { session_id }) = stream.next().await.transpose()? else {
            return Err(LlmFacadeError::Protocol);
        };
        Ok(Self {
            client,
            session_id,
            stream,
        })
    }

    async fn command<T: DeserializeOwned>(
        &mut self,
        command: &str,
        body: &impl Serialize,
    ) -> Result<T, LlmFacadeError> {
        self.client
            .post(&format!("/sessions/{}/{command}", self.session_id), body)
            .await?
            .json()
            .await
            .map_err(|_| LlmFacadeError::Protocol)
    }

    /// Reads one Target commit; an early EOF cannot masquerade as successful generation.
    pub async fn next_commit(&mut self) -> Result<TargetCommit, LlmFacadeError> {
        match self.stream.next().await.transpose()? {
            Some(RoleEvent::Committed(commit)) => Ok(commit),
            _ => Err(LlmFacadeError::Protocol),
        }
    }

    /// Requests one Draft candidate chain for the currently confirmed generation.
    pub async fn propose(
        &mut self,
        version: u64,
        max_tokens: u32,
    ) -> Result<Vec<u32>, LlmFacadeError> {
        #[derive(Deserialize)]
        struct Candidate {
            version: u64,
            token_ids: Vec<u32>,
        }
        let candidate: Candidate = self
            .command(
                "propose",
                &serde_json::json!({
                    "version": version, "max_tokens": max_tokens
                }),
            )
            .await?;
        if candidate.version != version || candidate.token_ids.len() > max_tokens as usize {
            return Err(LlmFacadeError::Protocol);
        }
        Ok(candidate.token_ids)
    }

    /// Applies the Target's exact delta before the frontend requests another proposal.
    pub async fn commit(
        &mut self,
        base_version: u64,
        version: u64,
        token_ids: &[u32],
    ) -> Result<(), LlmFacadeError> {
        #[derive(Deserialize)]
        struct Committed {
            version: u64,
        }
        let committed: Committed = self
            .command(
                "commit",
                &serde_json::json!({
                    "base_version": base_version, "version": version, "token_ids": token_ids
                }),
            )
            .await?;
        if committed.version != version {
            return Err(LlmFacadeError::Protocol);
        }
        Ok(())
    }

    /// Submits candidates once; a rejected generation ends this non-replaying workflow.
    pub async fn verify(&mut self, version: u64, token_ids: &[u32]) -> Result<(), LlmFacadeError> {
        #[derive(Deserialize)]
        struct Admission {
            accepted: bool,
        }
        let admission: Admission = self
            .command(
                "verify",
                &serde_json::json!({
                    "version": version, "token_ids": token_ids
                }),
            )
            .await?;
        if !admission.accepted {
            return Err(LlmFacadeError::Rejected);
        }
        Ok(())
    }
}
