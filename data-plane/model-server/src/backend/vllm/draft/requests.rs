// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Maps confirmed Target turns to retained native EngineCore requests and their private KV.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use futures::{StreamExt, future::try_join_all};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::Semaphore;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;
use vllm_engine_core_client::protocol::output::EngineCoreFinishReason;
use vllm_engine_core_client::protocol::request::EngineCoreRequest;
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;

use super::DraftClient;

#[derive(Debug, Deserialize, Serialize)]
pub(super) struct OpenPeer {
    pub peer_id: String,
    pub vocab_size: u32,
    pub num_features: usize,
    pub max_model_len: usize,
    pub max_requests: usize,
    pub num_candidates: usize,
    pub logits: Option<Value>,
    // The native worker owns validation of tensor layouts, methods and transport settings.
    #[serde(flatten)]
    pub worker_parameters: BTreeMap<String, Value>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ProposalRequest {
    request_id: String,
    cache_salt: Option<String>,
    generation: usize,
    base_length: usize,
    token_ids: Vec<u32>,
    context_start: usize,
    feature_offset: usize,
    feature_count: usize,
    temperature: f32,
    seed: i64,
    generate: bool,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ProposeBatch {
    sequence: u64,
    requests: Vec<ProposalRequest>,
}

#[derive(Serialize)]
pub(super) struct Proposal {
    sequence: u64,
    token_ids: Vec<Vec<u32>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FinishRequests {
    pub request_ids: Vec<String>,
}

#[derive(Deserialize)]
#[serde(tag = "operation", content = "payload", rename_all = "snake_case")]
pub(super) enum PeerCommand {
    Propose(ProposeBatch),
    Finish(FinishRequests),
    Close,
}

struct Session {
    native_id: String,
    pending_tokens: Vec<u32>,
    confirmed_length: usize,
    context_length: usize,
}

#[derive(Default)]
struct PeerRequests {
    sequence: u64,
    sessions: HashMap<String, Session>,
}

/// Retains sessions between bounded turns; the permit serializes use of one worker's IO buffers.
pub(super) struct Peer {
    pub spec: OpenPeer,
    requests: Mutex<PeerRequests>,
    pub operation: Arc<Semaphore>,
    pub closing: AtomicBool,
    pub closed: CancellationToken,
}

impl Peer {
    pub fn new(spec: OpenPeer) -> Self {
        Self {
            spec,
            requests: Mutex::new(PeerRequests::default()),
            operation: Arc::new(Semaphore::new(1)),
            closing: AtomicBool::new(false),
            closed: CancellationToken::new(),
        }
    }

    pub fn active_sessions(&self) -> usize {
        self.requests.lock().unwrap().sessions.len()
    }

    /// Delivers one received batch through native admission, scheduling, sampling and output dispatch.
    pub async fn propose(
        &self,
        client: &DraftClient,
        batch: ProposeBatch,
    ) -> Result<Proposal, String> {
        let neural = self.spec.num_features != 0;
        let capture_q = self.spec.logits.is_some();
        let num_turns = batch
            .requests
            .iter()
            .filter(|r| r.generate && r.generation < self.spec.max_model_len)
            .count();
        let stage_with_execution = neural && !capture_q && num_turns == batch.requests.len();
        let batch_id = format!("{}:{}", self.spec.peer_id, batch.sequence);
        let mut staged = Vec::with_capacity(batch.requests.len());
        let mut turns = Vec::with_capacity(batch.requests.len());
        {
            let mut state = self.requests.lock().unwrap();
            if batch.sequence != state.sequence {
                return Err("DT proposal sequence is out of order".into());
            }
            if batch.requests.len() > self.spec.max_requests {
                return Err("DT batch exceeds its registered request capacity".into());
            }
            let mut identifiers = HashSet::new();
            for request in &batch.requests {
                if !identifiers.insert(&request.request_id) {
                    return Err("DT batch contains duplicate requests".into());
                }
                if !state.sessions.contains_key(&request.request_id) {
                    if !client.accepting() {
                        return Err(
                            "Draft is draining; this request has no retained session".into()
                        );
                    }
                    state.sessions.insert(
                        request.request_id.clone(),
                        Session {
                            native_id: Uuid::new_v4().simple().to_string(),
                            pending_tokens: Vec::new(),
                            confirmed_length: 0,
                            context_length: 0,
                        },
                    );
                }
                let session = state.sessions.get_mut(&request.request_id).unwrap();
                if request.base_length != session.confirmed_length
                    || request.generation.checked_sub(request.base_length)
                        != Some(request.token_ids.len())
                {
                    return Err("DT token delta does not extend its confirmed prefix".into());
                }
                if request.context_start != session.context_length {
                    return Err("DT feature delta does not extend its confirmed prefix".into());
                }
                if request
                    .token_ids
                    .iter()
                    .any(|token| *token >= self.spec.vocab_size)
                {
                    return Err("DT confirmed token is outside the shared vocabulary".into());
                }
                session.pending_tokens.extend_from_slice(&request.token_ids);
                session.confirmed_length = request.generation;
                session.context_length += request.feature_count;
                if neural {
                    if session.context_length > session.confirmed_length {
                        return Err("DT feature history extends beyond confirmed tokens".into());
                    }
                    if request.generate
                        && session.context_length.checked_add(1) != Some(session.confirmed_length)
                    {
                        return Err("DT proposal is missing the confirmed Target features".into());
                    }
                } else if request.feature_count != 0 {
                    return Err("Ordinary Drafts do not consume Target features".into());
                }
                if session.confirmed_length == 0
                    || session.confirmed_length > self.spec.max_model_len
                {
                    return Err("DT confirmed prefix is empty or exceeds the context limit".into());
                }
                staged.push((
                    session.native_id.clone(),
                    request.context_start,
                    request.feature_offset,
                    request.feature_count,
                    request.generate,
                ));
                let count = self
                    .spec
                    .num_candidates
                    .min(self.spec.max_model_len - session.confirmed_length);
                if !request.generate || count == 0 {
                    turns.push(None);
                    continue;
                }
                if !request.temperature.is_finite() || request.temperature < 0.0 {
                    return Err("DT temperature must be finite and nonnegative".into());
                }
                let mut extra_args = HashMap::from([
                    ("dt_session".into(), json!(session.native_id)),
                    ("dt_batch".into(), json!(batch_id)),
                    ("dt_batch_size".into(), json!(num_turns)),
                    ("dt_generation".into(), json!(request.generation)),
                    (
                        "dt_anchor_token".into(),
                        json!(
                            session
                                .pending_tokens
                                .last()
                                .ok_or("DT proposal has no confirmed input")?
                        ),
                    ),
                    ("dt_capture_q".into(), json!(capture_q)),
                ]);
                if stage_with_execution {
                    extra_args.insert(
                        "dt_features".into(),
                        json!((
                            &self.spec.peer_id,
                            request.context_start,
                            request.feature_offset,
                            request.feature_count
                        )),
                    );
                }
                // EngineCore owns the complete prefix. Retain only input not yet submitted,
                // including a neural turn's anchor until its Target features arrive.
                let prompt_length = session.pending_tokens.len() - usize::from(neural);
                let prompt = session.pending_tokens.drain(..prompt_length).collect();
                // No EOS or stop IDs are supplied: each native turn must produce its bounded candidates.
                let params = EngineCoreSamplingParams {
                    temperature: request.temperature,
                    seed: Some((request.seed ^ 0x4452414654) & i64::MAX),
                    max_tokens: count as u32,
                    extra_args: Some(extra_args),
                    ..Default::default()
                };
                turns.push(Some(EngineCoreRequest {
                    request_id: session.native_id.clone(),
                    external_req_id: Some(session.native_id.clone()),
                    prompt_token_ids: Some(prompt),
                    sampling_params: Some(params),
                    cache_salt: request.cache_salt.clone(),
                    arrival_time: SystemTime::now()
                        .duration_since(UNIX_EPOCH)
                        .expect("system clock predates Unix epoch")
                        .as_secs_f64(),
                    resumable: true,
                    ..Default::default()
                }));
            }
        }
        if !stage_with_execution && (neural || capture_q) {
            client
                .rpc::<(), _>("dt_stage", (&self.spec.peer_id, &staged))
                .await?;
        }
        // Each native output stream covers one turn. The scheduler retains its request and KV by ID.
        let results = try_join_all(turns.into_iter().map(|turn| async move {
            let Some(turn) = turn else {
                return Ok((vec![0; self.spec.num_candidates], None));
            };
            let count = turn.sampling_params.as_ref().unwrap().max_tokens as usize;
            let id = turn.request_id.clone();
            let mut output = client
                .engine()
                .call(turn)
                .await
                .map_err(|e| e.to_string())?;
            let mut tokens = Vec::with_capacity(self.spec.num_candidates);
            while let Some(next) = output.next().await {
                let next = next.map_err(|e| e.to_string())?;
                tokens.extend_from_slice(&next.new_token_ids);
                if let Some(reason) = next.finish_reason {
                    if reason != EngineCoreFinishReason::Length || tokens.len() != count {
                        return Err("Native Draft returned an incomplete candidate sequence".into());
                    }
                    tokens.resize(self.spec.num_candidates, 0);
                    return Ok((tokens, Some((id, count))));
                }
            }
            Err("Native Draft output closed before its turn completed".to_owned())
        }))
        .await?;
        if capture_q {
            let proposals: Vec<_> = results.iter().map(|(_, proposal)| proposal).collect();
            client
                .rpc::<(), _>("dt_publish_probabilities", (&self.spec.peer_id, proposals))
                .await?;
        }
        self.requests.lock().unwrap().sequence += 1;
        Ok(Proposal {
            sequence: batch.sequence,
            token_ids: results.into_iter().map(|(tokens, _)| tokens).collect(),
        })
    }

    /// Joins native cancellation and device work before releasing confirmed input history.
    pub async fn finish(
        &self,
        client: &DraftClient,
        request_ids: Option<&[String]>,
    ) -> Result<(), String> {
        let sessions: Vec<_> = self
            .requests
            .lock()
            .unwrap()
            .sessions
            .iter()
            .filter(|(key, _)| request_ids.is_none_or(|ids| ids.contains(key)))
            .map(|(key, session)| (key.clone(), session.native_id.clone()))
            .collect();
        if sessions.is_empty() {
            return Ok(());
        }
        let native_ids: Vec<_> = sessions.iter().map(|(_, id)| id).collect();
        // A completed turn is absent from the client's in-flight registry but remains in Scheduler.
        // The native utility acknowledges scheduler abort before the Worker fence releases IO state.
        client
            .engine()
            .call_utility::<(), _>("abort_requests", (&native_ids,))
            .await
            .map_err(|e| e.to_string())?;
        client
            .rpc::<(), _>("dt_wait_execution", Vec::<Value>::new())
            .await?;
        client
            .rpc::<(), _>("dt_forget_sessions", (&native_ids,))
            .await?;
        let mut state = self.requests.lock().unwrap();
        for (key, _) in sessions {
            state.sessions.remove(&key);
        }
        Ok(())
    }

    pub fn in_flight(&self) -> bool {
        self.operation.available_permits() == 0 || self.closing.load(Ordering::Acquire)
    }
}
