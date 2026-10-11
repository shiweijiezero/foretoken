// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Internal role transport. The frontend, not either model, orders drafting and verification.

use std::pin::Pin;

use futures::{Stream, StreamExt};
use serde::{Deserialize, Serialize, de::DeserializeOwned};

use crate::LlmFacadeError;
use crate::http::{classify_reqwest, classify_status, is_ndjson, ndjson_lines, validate_endpoint};

/// Token-input generation request; the caller owns tokenization and model compatibility.
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
    #[serde(default)]
    pub sampling: DraftTargetSampling,
}

/// Explicit sampling choices understood by both independent roles.
#[derive(Clone, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct DraftTargetSampling {
    pub temperature: f32,
    pub top_p: f32,
    pub top_k: u32,
    pub seed: Option<i64>,
}

impl Default for DraftTargetSampling {
    fn default() -> Self {
        Self {
            temperature: 0.0,
            top_p: 1.0,
            top_k: 0,
            seed: None,
        }
    }
}

/// Draft artifact handle; Target must claim it before obtaining the GPU descriptor.
#[derive(Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ProposalArtifact {
    pub artifact_id: String,
}

/// Source-session-owned KV copy. The destination claims its artifact before reading.
#[derive(Serialize, Deserialize)]
pub struct KvCheckpoint {
    artifact_id: String,
    pub token_ids: Vec<u32>,
    pub computed_tokens: usize,
    layout: Vec<KvLayer>,
}

#[derive(Serialize, Deserialize)]
struct KvLayer {
    layer: String,
    shape: Vec<usize>,
    dtype: String,
}

/// Candidates and their optional distribution handle for one confirmed prefix.
#[derive(Deserialize)]
pub struct DraftProposal {
    pub version: u64,
    pub token_ids: Vec<u32>,
    pub artifact: Option<ProposalArtifact>,
    pub observation: foretoken_model_protocol::DraftTargetEngineObservation,
}

/// One member's ready candidates, passed to the selected Target with its sibling members.
#[derive(Serialize)]
pub struct DraftVerification {
    session_id: String,
    version: u64,
    token_ids: Vec<u32>,
    artifact: Option<ProposalArtifact>,
    source_endpoint: String,
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
    pub observation: foretoken_model_protocol::DraftTargetEngineObservation,
    pub transfer: Option<foretoken_model_protocol::DraftTargetTransferObservation>,
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
    AdmissionClosed,
    Opened { session_id: String },
    Committed(Box<TargetCommit>),
}

type RoleStream = Pin<Box<dyn Stream<Item = Result<RoleEvent, LlmFacadeError>> + Send>>;

/// A role's current admission and candidate capabilities, queried before binding.
#[derive(Clone, Deserialize)]
pub struct RoleStatus {
    /// Identifies Target-dependent proposal inputs; both selected roles must agree.
    pub context_profile: Option<serde_json::Value>,
    #[serde(default)]
    pub kv_migration: bool,
    #[serde(default)]
    pub prepared_tokenizer: Option<foretoken_model_protocol::PreparedTokenizer>,
    /// Omitted by lightweight readiness probes; full status includes native observations.
    pub workload: Option<foretoken_model_protocol::DraftTargetWorkload>,
    pub role: String,
    pub model: String,
    pub revision: Option<String>,
    pub tokenizer: String,
    pub tokenizer_revision: Option<String>,
    pub max_model_len: u32,
    pub accepting: bool,
    pub token_budget: u32,
    pub candidate_format: foretoken_model_protocol::DraftTargetCandidateFormat,
}

/// Target session address passed to Draft; the request state retains session ownership.
#[derive(serde::Serialize)]
pub struct TargetContextSource {
    endpoint: String,
    session_id: String,
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

    /// Identifies the selected endpoint when a round reuses an existing role session.
    pub fn endpoint(&self) -> &str {
        &self.endpoint
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

    /// Opens Target generation; the session owns cancellation through its response body.
    /// None means explicit refusal before session creation or inference.
    pub async fn generate(
        &self,
        request: &DraftTargetRequest,
    ) -> Result<Option<RoleSession>, LlmFacadeError> {
        RoleSession::open(self.clone(), self.post("/generate", request).await?).await
    }

    /// Opens a destination stream that imports KV before generating its first confirmed token.
    /// The caller retains the source session until this stream confirms successful execution.
    pub async fn resume(
        &self,
        request: &DraftTargetRequest,
        source: &RoleClient,
        checkpoint: &KvCheckpoint,
    ) -> Result<RoleSession, LlmFacadeError> {
        RoleSession::open(self.clone(), self.post("/resume", &serde_json::json!({
            "generation": request, "source_endpoint": source.endpoint, "checkpoint": checkpoint,
        })).await?).await?.ok_or(LlmFacadeError::Unavailable)
    }

    /// Submits one ready batch; results correspond to input order, including cancelled members.
    pub async fn verify_batch(
        &self,
        members: &[DraftVerification],
    ) -> Result<Vec<bool>, LlmFacadeError> {
        #[derive(Deserialize)]
        struct Admission {
            accepted: Vec<bool>,
        }
        let result: Admission = self
            .post("/batch/verify", &serde_json::json!({"members": members}))
            .await?
            .json()
            .await
            .map_err(|_| LlmFacadeError::Protocol)?;
        if result.accepted.len() != members.len() {
            return Err(LlmFacadeError::Protocol);
        }
        Ok(result.accepted)
    }

    /// Binds Draft to confirmed tokens and keeps the owning control response open.
    /// Returns None only when Draft explicitly closed admission before creating a session.
    pub async fn bind(
        &self,
        token_ids: &[u32],
        version: u64,
        sampling: &DraftTargetSampling,
    ) -> Result<Option<RoleSession>, LlmFacadeError> {
        RoleSession::open(
            self.clone(),
            self.post(
                "/sessions",
                &serde_json::json!({
                    "token_ids": token_ids, "version": version, "sampling": sampling
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
    /// Exports the observed paused generation; dropping this session revokes unclaimed copies.
    pub async fn checkpoint(&mut self, version: u64) -> Result<KvCheckpoint, LlmFacadeError> {
        self.command("checkpoint", &serde_json::json!({"version": version}))
            .await
    }
    async fn open(
        client: RoleClient,
        response: reqwest::Response,
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
        let session_id = match stream.next().await.transpose()? {
            Some(RoleEvent::Opened { session_id }) => session_id,
            Some(RoleEvent::AdmissionClosed) => return Ok(None),
            _ => return Err(LlmFacadeError::Protocol),
        };
        Ok(Some(Self {
            client,
            session_id,
            stream,
        }))
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

    /// Queries the current engine residency for a scheduler considering relocation.
    /// This snapshot can change immediately through native preemption; it grants no KV lease.
    pub async fn kv(
        &self,
    ) -> Result<foretoken_model_protocol::DraftTargetKvObservation, LlmFacadeError> {
        let response = self
            .client
            .client
            .get(format!(
                "{}/sessions/{}/kv",
                self.client.endpoint, self.session_id
            ))
            .send()
            .await
            .map_err(classify_reqwest)?;
        if !response.status().is_success() {
            return Err(classify_status(response.status()));
        }
        response.json().await.map_err(|_| LlmFacadeError::Protocol)
    }

    /// Reads one Target commit; an early EOF cannot masquerade as successful generation.
    pub async fn next_commit(&mut self) -> Result<TargetCommit, LlmFacadeError> {
        match self.stream.next().await.transpose()? {
            Some(RoleEvent::Committed(commit)) => Ok(*commit),
            _ => Err(LlmFacadeError::Protocol),
        }
    }

    /// Returns an owned context address for the frontend's next Draft proposal.
    pub fn context_source(&self) -> TargetContextSource {
        TargetContextSource {
            endpoint: self.client.endpoint().to_owned(),
            session_id: self.session_id.clone(),
        }
    }

    /// Requests one Draft candidate chain for the currently confirmed generation.
    pub async fn propose(
        &mut self,
        version: u64,
        max_tokens: u32,
        context_source: TargetContextSource,
    ) -> Result<DraftProposal, LlmFacadeError> {
        let candidate: DraftProposal = self
            .command(
                "propose",
                &serde_json::json!({
                    "version": version, "max_tokens": max_tokens,
                    "context_source": context_source,
                }),
            )
            .await?;
        if candidate.version != version || candidate.token_ids.len() > max_tokens as usize {
            return Err(LlmFacadeError::Protocol);
        }
        Ok(candidate)
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

    /// Identifies this member's proposal without transferring the owning session stream.
    pub fn verification(&self, proposal: DraftProposal, source: &RoleClient) -> DraftVerification {
        DraftVerification {
            session_id: self.session_id.clone(),
            version: proposal.version,
            token_ids: proposal.token_ids,
            artifact: proposal.artifact,
            source_endpoint: source.endpoint.clone(),
        }
    }
}
