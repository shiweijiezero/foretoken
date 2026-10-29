// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Maps confirmed Target turns to retained native EngineCore requests and their private KV.

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use futures::{
    StreamExt,
    stream::{FuturesUnordered, select_all},
};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use tokio::sync::Semaphore;
use tokio_util::sync::CancellationToken;
use uuid::Uuid;
use vllm_engine_core_client::EngineCoreOutputStream;
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
    pub protocol: String,
    pub return_probabilities: bool,
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

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub(super) struct PrepareBatch {
    sequence: u64,
    feature_rows: usize,
    num_requests: usize,
    #[serde(default)]
    requests: Vec<DeviceRequest>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct DeviceRequest {
    request_id: String,
    query_tokens: usize,
    seq_len_upper_bound: usize,
    initial: Option<InitialDeviceRequest>,
}

#[derive(Debug, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
struct InitialDeviceRequest {
    prompt_token_ids: Vec<u32>,
    cache_salt: Option<String>,
    temperature: f32,
    seed: i64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct ExecuteBatch {
    sequence: u64,
}

/// Native scheduler input; the request remains the owner of its slot and KV reservation.
#[derive(Debug)]
struct ReservedTurn {
    request_id: String,
    turn_sequence: u64,
    query_tokens: usize,
    seq_len_upper_bound: usize,
    feature_offset: usize,
    peer_id: String,
    initial: bool,
}

/// Scheduler-confirmed Target input returned with a non-terminal device proposal.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct NativeTurnCompleted {
    sequence: u64,
    confirmed_length: usize,
    context_length: usize,
    token_ids_delta: Vec<u32>,
    anchor: u32,
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
    #[serde(skip_serializing_if = "Option::is_none")]
    token_ids: Option<Vec<Vec<u32>>>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct FinishRequests {
    pub request_ids: Vec<String>,
}

#[derive(Deserialize)]
#[serde(tag = "operation", content = "payload", rename_all = "snake_case")]
pub(super) enum PeerCommand {
    Connect,
    Prepare(PrepareBatch),
    Execute(ExecuteBatch),
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

/// Keeps an offered batch local until native admission confirms its request ownership.
struct PreparedDeviceTurn {
    plans: Vec<ReservedTurn>,
    previous: Vec<(String, usize)>,
    turns: Vec<EngineCoreRequest>,
    initial_sessions: Vec<(String, Session)>,
}

#[derive(Default)]
struct PeerRequests {
    sequence: u64,
    prepared: bool,
    execution: Option<tokio::task::JoinHandle<Result<(), String>>>,
    sessions: HashMap<String, Session>,
    // Device turns retain cancellation and output ownership across proposal boundaries.
    streams: HashMap<String, EngineCoreOutputStream>,
}

/// Collects one device proposal without ending its native request's output ownership.
async fn collect_device_turn(
    mut stream: EngineCoreOutputStream,
    sequence: u64,
    expected_tokens: usize,
) -> Result<(EngineCoreOutputStream, NativeTurnCompleted), String> {
    let mut count = 0;
    while let Some(output) = stream.next().await {
        let output = output.map_err(|error| error.to_string())?;
        count += output.new_token_ids.len();
        if output.finish_reason.is_some() || count > expected_tokens {
            return Err("Native Draft ended before its device proposal completed".into());
        }
        if let Some(payload) = &output.continuation_output {
            let completed: NativeTurnCompleted =
                rmpv::ext::from_value(payload.clone()).map_err(|error| error.to_string())?;
            if completed.sequence != sequence || count != expected_tokens {
                return Err(
                    "Native Draft returned an incomplete or out-of-order device proposal".into(),
                );
            }
            return Ok((stream, completed));
        }
    }
    Err("Native Draft output closed before its device turn completed".into())
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
    /// Creates one connection's session owner and serializes its native operations.
    pub fn new(spec: OpenPeer) -> Self {
        Self {
            spec,
            requests: Mutex::new(PeerRequests::default()),
            operation: Arc::new(Semaphore::new(1)),
            closing: AtomicBool::new(false),
            closed: CancellationToken::new(),
        }
    }

    /// Counts retained native sessions, including turns waiting for Target confirmation.
    pub fn active_sessions(&self) -> usize {
        self.requests.lock().unwrap().sessions.len()
    }

    /// Announces tensor shapes while Target computation is still producing their contents.
    pub async fn prepare(
        self: &Arc<Self>,
        client: &DraftClient,
        batch: PrepareBatch,
    ) -> Result<bool, String> {
        let prepared = self.prepare_device_turn(client, &batch)?;
        if !prepared.plans.is_empty()
            && self
                .start_device_turn(client, batch.sequence, prepared)
                .await?
        {
            return Ok(true);
        }
        client
            .rpc::<(), _>("dt_prepare", (&self.spec.peer_id, json!(batch)))
            .await?;
        self.requests.lock().unwrap().prepared = true;
        Ok(false)
    }

    /// Constructs native inputs without publishing new sessions before batch acceptance.
    fn prepare_device_turn(
        &self,
        client: &DraftClient,
        batch: &PrepareBatch,
    ) -> Result<PreparedDeviceTurn, String> {
        let state = self.requests.lock().unwrap();
        if batch.sequence != state.sequence || state.prepared {
            return Err("DT input preparation sequence is out of order".into());
        }
        if batch.num_requests > self.spec.max_requests {
            return Err("DT batch exceeds its registered request capacity".into());
        }
        let mut plans = Vec::new();
        let mut previous = Vec::new();
        let mut turns = Vec::new();
        let mut initial_sessions = Vec::new();
        if !batch.requests.is_empty() {
            if self.spec.protocol != "nccl" || self.spec.num_features == 0 {
                return Err("Device turns require a neural NCCL Draft".into());
            }
            if batch.requests.len() != batch.num_requests {
                return Err("DT device rows differ from the prepared batch".into());
            }
            let mut identifiers = HashSet::new();
            let mut offset = 0_usize;
            for request in &batch.requests {
                if !identifiers.insert(&request.request_id) || request.query_tokens == 0 {
                    return Err("DT device batch contains duplicate or empty rows".into());
                }
                let existing = state.sessions.get(&request.request_id);
                let (native_id, confirmed_length) = match (&request.initial, existing) {
                    (Some(initial), None) => {
                        if !client.accepting() {
                            return Err(
                                "Draft is draining; this request has no retained session".into()
                            );
                        }
                        if initial.prompt_token_ids.len() != request.query_tokens
                            || initial.prompt_token_ids.is_empty()
                            || initial
                                .prompt_token_ids
                                .iter()
                                .any(|token| *token >= self.spec.vocab_size)
                            || !initial.temperature.is_finite()
                            || initial.temperature < 0.0
                        {
                            return Err("DT initial device input has an invalid prompt or sampling configuration".into());
                        }
                        (
                            Uuid::new_v4().simple().to_string(),
                            initial.prompt_token_ids.len(),
                        )
                    }
                    (None, Some(session)) => (session.native_id.clone(), session.confirmed_length),
                    _ => {
                        plans.clear();
                        previous.clear();
                        turns.clear();
                        initial_sessions.clear();
                        break;
                    }
                };
                let plan = ReservedTurn {
                    request_id: native_id.clone(),
                    turn_sequence: batch.sequence,
                    query_tokens: request.query_tokens,
                    seq_len_upper_bound: request.seq_len_upper_bound,
                    feature_offset: offset,
                    peer_id: self.spec.peer_id.clone(),
                    initial: request.initial.is_some(),
                };
                if !state.streams.contains_key(&native_id) {
                    let mut extra = HashMap::from([
                        ("dt_session".into(), json!(native_id)),
                        (
                            "dt_reserved_turn".into(),
                            json!({
                                "turn_sequence": plan.turn_sequence,
                                "query_tokens": plan.query_tokens,
                                "seq_len_upper_bound": plan.seq_len_upper_bound,
                                "feature_offset": plan.feature_offset,
                                "peer_id": plan.peer_id,
                                "initial": plan.initial,
                            }),
                        ),
                    ]);
                    let (prompt, cache_salt, temperature, seed) =
                        if let Some(initial) = &request.initial {
                            extra.extend([
                                ("dt_generation".into(), json!(confirmed_length)),
                                ("dt_capture_q".into(), json!(self.spec.return_probabilities)),
                                ("dt_gpu_output".into(), json!(true)),
                            ]);
                            initial_sessions.push((
                                request.request_id.clone(),
                                Session {
                                    native_id: native_id.clone(),
                                    pending_tokens: Vec::new(),
                                    confirmed_length,
                                    context_length: 0,
                                },
                            ));
                            (
                                initial.prompt_token_ids.clone(),
                                initial.cache_salt.clone(),
                                if self.spec.return_probabilities {
                                    initial.temperature
                                } else {
                                    0.0
                                },
                                Some((initial.seed ^ 0x4452414654) & i64::MAX),
                            )
                        } else {
                            (Vec::new(), None, 0.0, None)
                        };
                    turns.push(EngineCoreRequest {
                        request_id: native_id.clone(),
                        external_req_id: Some(native_id.clone()),
                        prompt_token_ids: Some(prompt),
                        sampling_params: Some(EngineCoreSamplingParams {
                            temperature,
                            seed,
                            max_tokens: self.spec.num_candidates as u32,
                            extra_args: Some(extra),
                            ..Default::default()
                        }),
                        cache_salt,
                        arrival_time: SystemTime::now()
                            .duration_since(UNIX_EPOCH)
                            .expect("system clock predates Unix epoch")
                            .as_secs_f64(),
                        resumable: true,
                        ..Default::default()
                    });
                }
                previous.push((native_id, confirmed_length));
                plans.push(plan);
                offset = offset
                    .checked_add(request.query_tokens)
                    .ok_or("DT device batch size overflowed")?;
            }
            if !plans.is_empty() && offset != batch.feature_rows {
                return Err("DT device rows differ from the announced feature layout".into());
            }
        }
        Ok(PreparedDeviceTurn {
            plans,
            previous,
            turns,
            initial_sessions,
        })
    }

    /// Registers external inputs before Target sampling and retains their native completion owner.
    async fn start_device_turn(
        self: &Arc<Self>,
        client: &DraftClient,
        sequence: u64,
        prepared: PreparedDeviceTurn,
    ) -> Result<bool, String> {
        let PreparedDeviceTurn {
            plans,
            previous,
            turns,
            initial_sessions,
        } = prepared;
        let rows: Vec<_> = plans
            .iter()
            .map(|plan| {
                (
                    &plan.request_id,
                    plan.query_tokens,
                    plan.seq_len_upper_bound,
                    plan.initial,
                )
            })
            .collect();
        let admission: Vec<_> = plans
            .iter()
            .map(|plan| {
                json!({
                    "request_id": plan.request_id,
                    "turn_sequence": plan.turn_sequence,
                    "query_tokens": plan.query_tokens,
                    "seq_len_upper_bound": plan.seq_len_upper_bound,
                    "feature_offset": plan.feature_offset,
                    "peer_id": plan.peer_id,
                    "initial": plan.initial,
                })
            })
            .collect();
        let engine_index = self
            .requests
            .lock()
            .unwrap()
            .streams
            .values()
            .next()
            .map(EngineCoreOutputStream::engine_index);
        let streams = client
            .engine()
            .try_call_batch(
                turns,
                engine_index,
                Some(("dt_execute", (&self.spec.peer_id, sequence, rows))),
                ("prepare_batch_admission", admission),
            )
            .await
            .map_err(|error| error.to_string())?;
        let Some(streams) = streams else {
            return Ok(false);
        };
        // Native registration owns every row; retain the same output stream until
        // a terminal fallback or final cancellation ends that ownership.
        let streams = {
            let mut state = self.requests.lock().unwrap();
            state.sessions.extend(initial_sessions);
            for stream in streams {
                state.streams.insert(stream.request_id().to_owned(), stream);
            }
            previous
                .iter()
                .map(|(id, _)| {
                    state.streams.remove(id).ok_or_else(|| {
                        "Accepted device turn has no native output stream".to_owned()
                    })
                })
                .collect::<Result<Vec<_>, _>>()?
        };
        let peer = self.clone();
        let client = client.clone();
        let execution = tokio::spawn(async move {
            let result: Result<(), String> = async {
                let mut pending: FuturesUnordered<_> = streams
                    .into_iter()
                    .map(|stream| collect_device_turn(stream, sequence, peer.spec.num_candidates))
                    .collect();
                let mut confirmed = HashMap::with_capacity(previous.len());
                while let Some(turn) = pending.next().await {
                    let (stream, completed) = turn?;
                    let native_id = stream.request_id().to_owned();
                    confirmed.insert(native_id.clone(), completed);
                    peer.requests
                        .lock()
                        .unwrap()
                        .streams
                        .insert(native_id, stream);
                }
                peer.reconcile_device_turn(&previous, confirmed)
            }
            .await;
            if let Err(error) = &result {
                client.fail(error.clone());
            }
            result
        });
        let mut state = self.requests.lock().unwrap();
        state.execution = Some(execution);
        state.prepared = true;
        Ok(true)
    }

    /// Joins the native device turn before acknowledging reuse of its registered buffers.
    pub async fn execute(&self, batch: ExecuteBatch) -> Result<Proposal, String> {
        let execution = {
            let mut state = self.requests.lock().unwrap();
            if batch.sequence != state.sequence {
                return Err("DT device execution sequence is out of order".into());
            }
            state
                .execution
                .take()
                .ok_or("DT device execution was not reserved")?
        };
        execution.await.map_err(|error| error.to_string())??;
        let mut state = self.requests.lock().unwrap();
        state.sequence += 1;
        state.prepared = false;
        Ok(Proposal {
            sequence: batch.sequence,
            token_ids: None,
        })
    }

    /// Applies native request truth after device execution, retaining only its unsubmitted anchor.
    fn reconcile_device_turn(
        &self,
        previous: &[(String, usize)],
        confirmed: HashMap<String, NativeTurnCompleted>,
    ) -> Result<(), String> {
        if confirmed.len() != previous.len() {
            return Err("Native Draft returned incomplete device confirmation state".into());
        }
        let mut state = self.requests.lock().unwrap();
        for (native_id, before) in previous {
            let confirmed = confirmed
                .get(native_id)
                .ok_or("Native Draft did not confirm its retained session")?;
            if confirmed.confirmed_length.checked_sub(*before)
                != Some(confirmed.token_ids_delta.len())
                || confirmed.context_length.checked_add(1) != Some(confirmed.confirmed_length)
                || confirmed.anchor >= self.spec.vocab_size
                || confirmed.confirmed_length > self.spec.max_model_len
            {
                return Err("Native Draft returned inconsistent device confirmation state".into());
            }
            let session = state
                .sessions
                .values_mut()
                .find(|session| session.native_id == *native_id)
                .ok_or("Native Draft confirmed an unknown retained session")?;
            session.confirmed_length = confirmed.confirmed_length;
            session.context_length = confirmed.context_length;
            session.pending_tokens.clear();
            session.pending_tokens.push(confirmed.anchor);
        }
        Ok(())
    }

    /// Delivers one received batch through native admission, scheduling, sampling and output dispatch.
    pub async fn propose(
        &self,
        client: &DraftClient,
        batch: ProposeBatch,
    ) -> Result<Proposal, String> {
        let neural = self.spec.num_features != 0;
        let capture_q = self.spec.return_probabilities;
        let gpu_output = self.spec.protocol == "nccl";
        let num_turns = batch
            .requests
            .iter()
            .filter(|r| r.generate && r.generation < self.spec.max_model_len)
            .count();
        let stage_with_execution =
            neural && !capture_q && !gpu_output && num_turns == batch.requests.len();
        let mut staged = Vec::with_capacity(batch.requests.len());
        let mut turns = Vec::with_capacity(batch.requests.len());
        {
            let mut state = self.requests.lock().unwrap();
            if batch.sequence != state.sequence || state.execution.is_some() {
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
                let count = if request.generate {
                    self.spec
                        .num_candidates
                        .min(self.spec.max_model_len - session.confirmed_length)
                } else {
                    0
                };
                staged.push((
                    session.native_id.clone(),
                    request.context_start,
                    request.feature_offset,
                    request.feature_count,
                    count,
                ));
                if count == 0 {
                    turns.push(None);
                    continue;
                }
                if !request.temperature.is_finite() || request.temperature < 0.0 {
                    return Err("DT temperature must be finite and nonnegative".into());
                }
                let mut extra_args = HashMap::from([
                    ("dt_session".into(), json!(session.native_id)),
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
                    ("dt_gpu_output".into(), json!(gpu_output)),
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
        // Core admits the received batch before scheduling. Its existing streams
        // still own per-request cancellation and completion while one collector
        // restores the peer's row order.
        let mut results = Vec::with_capacity(turns.len());
        let mut pending = HashMap::with_capacity(num_turns);
        let mut requests = Vec::with_capacity(num_turns);
        for (row, turn) in turns.into_iter().enumerate() {
            if let Some(turn) = turn {
                let count = turn.sampling_params.as_ref().unwrap().max_tokens as usize;
                pending.insert(turn.request_id.clone(), (row, count));
                results.push((Vec::with_capacity(self.spec.num_candidates), None));
                requests.push(turn);
            } else {
                results.push((vec![0; self.spec.num_candidates], None));
            }
        }
        let (requests, updates, mut streams) = {
            let mut state = self.requests.lock().unwrap();
            let mut additions = Vec::new();
            let mut updates = Vec::new();
            let mut streams = Vec::new();
            for request in requests {
                if let Some(stream) = state.streams.remove(&request.request_id) {
                    updates.push(request);
                    streams.push(stream);
                } else {
                    additions.push(request);
                }
            }
            (additions, updates, streams)
        };
        let prepare_inputs = !stage_with_execution && (neural || capture_q || gpu_output);
        if prepare_inputs && pending.is_empty() {
            client
                .rpc::<(), _>("dt_stage", (&self.spec.peer_id, &staged))
                .await?;
        }
        let worker_rpc = (prepare_inputs && !pending.is_empty())
            .then_some(("dt_stage", (&self.spec.peer_id, &staged)));
        streams.extend(
            client
                .engine()
                .call_batch(requests, updates, worker_rpc)
                .await
                .map_err(|error| error.to_string())?,
        );
        let mut output = select_all(streams);
        while let Some(next) = output.next().await {
            let next = next.map_err(|error| error.to_string())?;
            let &(row, count) = pending
                .get(&next.request_id)
                .ok_or("Native Draft returned an unknown turn")?;
            let (tokens, proposal) = &mut results[row];
            tokens.extend_from_slice(&next.new_token_ids);
            if let Some(reason) = next.finish_reason {
                if reason != EngineCoreFinishReason::Length || tokens.len() != count {
                    return Err("Native Draft returned an incomplete candidate sequence".into());
                }
                tokens.resize(self.spec.num_candidates, 0);
                *proposal = Some((next.request_id.clone(), count));
                pending.remove(&next.request_id);
            }
        }
        if !pending.is_empty() {
            return Err("Native Draft output closed before its turn completed".into());
        }
        if capture_q && !gpu_output {
            let proposals: Vec<_> = results.iter().map(|(_, proposal)| proposal).collect();
            client
                .rpc::<(), _>("dt_publish_probabilities", (&self.spec.peer_id, proposals))
                .await?;
        }
        {
            let mut state = self.requests.lock().unwrap();
            state.sequence += 1;
            state.prepared = false;
        }
        Ok(Proposal {
            sequence: batch.sequence,
            token_ids: (!gpu_output)
                .then(|| results.into_iter().map(|(tokens, _)| tokens).collect()),
        })
    }

    /// Joins native cancellation and device work before releasing confirmed input history.
    pub async fn finish(
        &self,
        client: &DraftClient,
        request_ids: Option<&[String]>,
    ) -> Result<(), String> {
        let prepared_sequence = {
            let state = self.requests.lock().unwrap();
            state.execution.as_ref().map(|_| state.sequence)
        };
        if let Some(sequence) = prepared_sequence {
            self.execute(ExecuteBatch { sequence }).await?;
        }
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
        // Scheduler retains both terminal fallback sessions and live device streams.
        // Abort native ownership before the Worker fence releases their input history.
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
        for (key, native_id) in sessions {
            state.streams.remove(&native_id);
            state.sessions.remove(&key);
        }
        Ok(())
    }

    /// Keeps prepared transfers and closing sessions visible until their ownership ends.
    pub fn in_flight(&self) -> bool {
        self.operation.available_permits() == 0
            || self.requests.lock().unwrap().prepared
            || self.closing.load(Ordering::Acquire)
    }
}
