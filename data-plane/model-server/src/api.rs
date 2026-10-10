// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Restricted internal HTTP routes for already-tokenized EngineCore requests.

use std::convert::Infallible;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::body::Body;
use axum::extract::rejection::JsonRejection;
use axum::extract::{DefaultBodyLimit, Query, State};
use axum::http::{HeaderMap, HeaderValue, StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Json, Router};
use bytes::Bytes;
use futures::StreamExt;
use serde::{Deserialize, Serialize};

use crate::backend::{Backend, BackendError, GenerateInput, TokenEvent};
use crate::instance_admission::{InstanceAdmissionConfig, InstanceAdmissionStatus};
use crate::kv_event_adapter::{KvDeltaError, KvEventAdapter};
use crate::runtime_cache;
use foretoken_model_protocol::{
    AbortInput, KV_INDEX_DELTA_PATH, KvDeltaQuery, RuntimeMetadataResponse, TelemetryResponse,
};

const OPENMETRICS_CONTENT_TYPE: &str = "application/openmetrics-text; version=1.0.0; charset=utf-8";

/// Engine state used for API health and admission only; process ownership stays upstream.
#[derive(Default)]
pub struct RuntimeHealth {
    process_alive: AtomicBool,
    client_healthy: AtomicBool,
    // Acceptance, configuration publication, and close share one linearization boundary.
    admission: Mutex<InstanceAdmissionStatus>,
    failure: tokio::sync::Notify,
    drained: tokio::sync::Notify,
    stopped: tokio::sync::Notify,
}

impl RuntimeHealth {
    /// Creates closed health and admission state for the model-server supervisor.
    pub fn new() -> Self {
        Self::default()
    }

    /// Initializes a protocol-0 startup limit before the supervisor opens ingress.
    /// This does not acknowledge support for projected live configuration.
    pub fn set_startup_admission_limit(&self, limit: Option<u32>) {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .max_concurrent_requests = limit;
    }

    /// Applies a newer or identical configuration without resetting occupancy or reopening admission.
    /// The configuration watcher calls this before acknowledging the controller's version.
    pub fn apply_instance_configuration(&self, configuration: InstanceAdmissionConfig) {
        let mut state = self
            .admission
            .lock()
            .expect("instance admission lock poisoned");
        let version = configuration.version.get();
        let limit = configuration
            .max_concurrent_requests
            .map(|limit| limit.get());
        if state.active_generation.is_some_and(|active| {
            version < active || version == active && state.max_concurrent_requests != limit
        }) {
            state.target_generation = Some(version);
            state.configuration_error =
                Some("instance admission version is stale or has different limits".into());
            return;
        }
        state.max_concurrent_requests = limit;
        state.active_generation = Some(version);
        state.target_generation = Some(version);
        state.configuration_error = None;
    }

    /// Reports a rejected candidate while retaining the last applied limit and permits.
    pub fn reject_instance_configuration(&self, version: Option<u64>, error: String) {
        let mut state = self
            .admission
            .lock()
            .expect("instance admission lock poisoned");
        if let Some(version) = version {
            state.target_generation = Some(version);
        }
        state.configuration_error = Some(error);
    }

    /// Returns one coherent configuration and occupancy observation for the controller.
    pub fn instance_admission_status(&self) -> InstanceAdmissionStatus {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .clone()
    }

    /// Requests supervised engine shutdown when a submitted request's completion becomes unknown.
    pub fn fail_execution(&self) {
        self.set_accepting(false);
        self.set_client_healthy(false);
        self.failure.notify_one();
    }

    /// Waits for an execution-lifecycle failure requiring the supervisor to stop the engine.
    pub async fn execution_failed(&self) {
        self.failure.notified().await;
    }

    /// Waits until every accepted execution has relinquished its instance slot.
    pub async fn wait_drained(&self) {
        loop {
            let drained = self.drained.notified();
            tokio::pin!(drained);
            drained.as_mut().enable();
            if self.running_requests() == 0 {
                return;
            }
            drained.await;
        }
    }

    /// Publishes managed-engine process liveness to probe handlers; the supervisor owns updates.
    pub fn set_process_alive(&self, value: bool) {
        self.process_alive.store(value, Ordering::Release);
        if !value {
            self.stopped.notify_waiters();
        }
    }

    /// Waits for the supervisor's confirmation that the managed engine has stopped.
    pub async fn wait_stopped(&self) {
        loop {
            let stopped = self.stopped.notified();
            tokio::pin!(stopped);
            stopped.as_mut().enable();
            if !self.process_alive.load(Ordering::Acquire) {
                return;
            }
            stopped.await;
        }
    }

    /// Publishes EngineCore client health to probe handlers; the supervisor owns updates.
    pub fn set_client_healthy(&self, value: bool) {
        self.client_healthy.store(value, Ordering::Release);
    }

    /// Opens or closes new HTTP admission; accepted executions retain their instance slots.
    pub fn set_accepting(&self, value: bool) {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .accepting = value;
    }
    /// Returns process-and-client health for the `/healthz` probe without changing lifecycle state.
    pub fn healthy(&self) -> bool {
        self.process_alive.load(Ordering::Acquire) && self.client_healthy.load(Ordering::Acquire)
    }

    /// Returns readiness for the `/readyz` probe; it currently shares the health lifecycle signal.
    pub fn ready(&self) -> bool {
        self.healthy()
    }

    /// Returns whether new requests may enter for telemetry and admission consumers.
    ///
    /// Execution tasks retain existing permits after this signal closes.
    pub fn accepting(&self) -> bool {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .accepting
    }
    /// Reserves an instance slot against the current limit; the execution task owns its release.
    pub fn try_admit(self: &Arc<Self>) -> Result<AdmissionPermit, AdmissionRejection> {
        let mut state = self
            .admission
            .lock()
            .expect("instance admission lock poisoned");
        if !state.accepting {
            return Err(AdmissionRejection::Closed);
        }
        if state
            .max_concurrent_requests
            .is_some_and(|limit| state.running_requests >= u64::from(limit))
        {
            return Err(AdmissionRejection::Busy);
        }
        state.running_requests += 1;
        Ok(AdmissionPermit {
            health: self.clone(),
        })
    }
    /// Returns the number of accepted requests still holding a permit.
    pub fn running_requests(&self) -> u64 {
        self.admission
            .lock()
            .expect("instance admission lock poisoned")
            .running_requests
    }
}

/// A definite rejection before any request is submitted to the inference engine.
#[derive(Debug, Clone, Copy)]
pub enum AdmissionRejection {
    Closed,
    Busy,
}

/// Instance slot retained until engine termination is confirmed.
pub struct AdmissionPermit {
    health: Arc<RuntimeHealth>,
}

impl Drop for AdmissionPermit {
    fn drop(&mut self) {
        self.health
            .admission
            .lock()
            .expect("instance admission lock poisoned")
            .running_requests -= 1;
        self.health.drained.notify_waiters();
    }
}

#[derive(Clone)]
struct ExecutionStore {
    ledger: Arc<foretoken_request_ledger::RequestLedger>,
    pod_uid: String,
    epoch: u64,
}

/// Mutable process state shared by typed HTTP handlers.
#[derive(Clone)]
pub struct AppState {
    backend: Arc<dyn Backend>,
    health: Arc<RuntimeHealth>,
    metadata: RuntimeMetadataResponse,
    kv_events: Option<Arc<KvEventAdapter>>,
    runtime_cache: Option<runtime_cache::Config>,
    profiling: Option<crate::profiling::Handle>,
    shared_kv: Option<crate::shared_kv::SharedKvLookup>,
    ledger: Option<ExecutionStore>,
}
impl AppState {
    /// Builds state consumed by internal HTTP handlers; the server owns the supplied backend state.
    pub fn new(
        backend: Arc<dyn Backend>,
        health: Arc<RuntimeHealth>,
        metadata: RuntimeMetadataResponse,
    ) -> Self {
        Self {
            backend,
            health,
            metadata,
            kv_events: None,
            runtime_cache: None,
            profiling: None,
            shared_kv: None,
            ledger: None,
        }
    }

    /// Attaches the platform capacity ledger used to accept and finish frontend reservations.
    pub fn with_request_ledger(
        mut self,
        ledger: Arc<foretoken_request_ledger::RequestLedger>,
        pod_uid: String,
        epoch: u64,
    ) -> Self {
        self.ledger = Some(ExecutionStore {
            ledger,
            pod_uid,
            epoch,
        });
        self
    }

    /// Attaches the shared KV delta source used by the index endpoint and returns updated state.
    ///
    /// The router owns this state while its handlers retain cloned adapter references.
    pub fn with_kv_events(mut self, adapter: Arc<KvEventAdapter>) -> Self {
        self.kv_events = Some(adapter);
        self
    }

    /// Enables read-only shared-prefix observations through the running connector.
    pub fn with_shared_kv(mut self, lookup: crate::shared_kv::SharedKvLookup) -> Self {
        self.shared_kv = Some(lookup);
        self
    }

    /// Attaches RuntimeCache filesystem telemetry rendered with backend metrics.
    pub fn with_runtime_cache(mut self, config: runtime_cache::Config) -> Self {
        self.runtime_cache = Some(config);
        self
    }

    /// Attaches diagnostic control on the existing internal listener without transferring supervision.
    pub fn with_profiling(mut self, handle: crate::profiling::Handle) -> Self {
        self.profiling = Some(handle);
        self
    }
}

/// Constructs the group-local API consumed by the Pod-local frontend, not an OpenAI router.
///
/// Server bootstrap moves `state` into the returned router, which owns it for all handler lifetimes.
pub fn router(state: AppState, internal_generate_request_body_limit_bytes: usize) -> Router {
    Router::new()
        .route("/healthz", get(healthz))
        .route("/readyz", get(readyz))
        .route("/metrics", get(metrics))
        .route("/v1/internal/metadata", get(metadata))
        .route("/v1/internal/telemetry", get(telemetry))
        .route("/v1/internal/admission", get(instance_admission_status))
        .route(
            "/v1/internal/admission/ready",
            get(instance_admission_ready),
        )
        .route("/v1/internal/admission/close", post(close_admission))
        .route(
            "/v1/internal/profiling",
            get(profile_observation).post(profile_control),
        )
        .route("/v1/internal/generate", post(generate))
        .route("/v1/internal/abort", post(abort))
        .route(KV_INDEX_DELTA_PATH, get(kv_index_delta))
        .route(
            foretoken_model_protocol::KV_SHARED_PREFIX_PATH,
            post(shared_kv_prefix),
        )
        .layer(DefaultBodyLimit::max(
            internal_generate_request_body_limit_bytes,
        ))
        .with_state(state)
}

// The protocol-1 workload probe cannot admit an older binary that ignores the projected limit.
async fn instance_admission_ready(State(state): State<AppState>) -> StatusCode {
    if state.health.ready()
        && state
            .health
            .instance_admission_status()
            .active_generation
            .is_some()
    {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

// The controller observes applied limits through the same listener used for drain and execution.
async fn instance_admission_status(State(state): State<AppState>) -> Json<InstanceAdmissionStatus> {
    Json(state.health.instance_admission_status())
}

#[derive(Deserialize)]
struct ProfileQuery {
    run_uid: Option<String>,
}

// Return runtime identity even before a run exists, so the reconciler can persist a fixed plan.
async fn profile_observation(
    State(state): State<AppState>,
    Query(query): Query<ProfileQuery>,
) -> Response {
    match state.profiling {
        Some(handle) => Json(handle.observe(query.run_uid.as_deref())).into_response(),
        None => StatusCode::NOT_IMPLEMENTED.into_response(),
    }
}

// The handler acknowledges acceptance only; polling exposes the independently supervised result.
async fn profile_control(
    State(state): State<AppState>,
    Json(request): Json<crate::profiling::Request>,
) -> Response {
    let Some(handle) = state.profiling else {
        return StatusCode::NOT_IMPLEMENTED.into_response();
    };
    let uid = request.run_uid.clone();
    match handle.control(request) {
        Ok(()) => (StatusCode::ACCEPTED, Json(handle.observe(Some(&uid)))).into_response(),
        Err(error) => (StatusCode::CONFLICT, error).into_response(),
    }
}

// Shared lookups are observations only: admission and engine health must still permit reads.
async fn shared_kv_prefix(
    State(state): State<AppState>,
    Json(request): Json<foretoken_model_protocol::KvSharedPrefixRequest>,
) -> Response {
    if !state.health.ready() || !state.health.accepting() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    let Some(lookup) = state.shared_kv else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    match tokio::time::timeout(
        foretoken_model_protocol::KV_OBSERVATION_TIMEOUT,
        lookup.lookup(&request),
    )
    .await
    {
        Ok(Some(response)) => Json(response).into_response(),
        _ => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
}

async fn healthz(State(state): State<AppState>) -> StatusCode {
    status(state.health.healthy())
}
async fn readyz(State(state): State<AppState>) -> StatusCode {
    status(state.health.ready())
}

async fn metrics(State(state): State<AppState>) -> Response {
    match state.backend.render_openmetrics() {
        Ok(mut body) => {
            if let Some(cache) = &state.runtime_cache {
                if let Some(without_eof) = body.strip_suffix("# EOF\n") {
                    body = without_eof.to_owned();
                }
                body.push_str(&cache.render_openmetrics());
                body.push_str("# EOF\n");
            }
            (
                [(
                    header::CONTENT_TYPE,
                    HeaderValue::from_static(OPENMETRICS_CONTENT_TYPE),
                )],
                body,
            )
                .into_response()
        }
        Err(_) => StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

async fn metadata(State(state): State<AppState>) -> Json<RuntimeMetadataResponse> {
    Json(state.metadata)
}

async fn telemetry(State(state): State<AppState>) -> Json<TelemetryResponse> {
    Json(telemetry_response(&state))
}

// The shutdown coordinator closes admission before draining. Return the post-close telemetry
// snapshot so its caller can observe remaining requests without owning server state.
async fn close_admission(State(state): State<AppState>) -> Json<TelemetryResponse> {
    state.health.set_accepting(false);
    Json(telemetry_response(&state))
}

fn telemetry_response(state: &AppState) -> TelemetryResponse {
    let telemetry = state.backend.telemetry();
    TelemetryResponse {
        version: 2,
        collected_at_unix_ms: SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_millis()
            .try_into()
            .unwrap_or(u64::MAX),
        accepting: state.health.accepting(),
        data_parallel_ranks: telemetry.data_parallel_ranks,
        running_requests: state
            .health
            .running_requests()
            .max(telemetry.running_requests),
        max_concurrent_requests: telemetry.max_concurrent_requests,
        scheduler_running_requests: telemetry.scheduler_running_requests,
        scheduler_waiting_requests: telemetry.scheduler_waiting_requests,
        kv_cache_usage: telemetry.kv_cache_usage,
        prompt_tokens_total: telemetry.prompt_tokens_total,
        generation_tokens_total: telemetry.generation_tokens_total,
        ttft_seconds: telemetry.ttft_seconds,
        tpot_seconds: telemetry.tpot_seconds,
        e2e_seconds: telemetry.e2e_seconds,
    }
}

/// Decodes an internal request and transfers its instance slot to the engine execution task.
async fn generate(
    State(state): State<AppState>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Response, ApiError> {
    // Multimodal tensors use MessagePack; ordinary requests keep a human-readable JSON boundary.
    let input: GenerateInput = if content_type_is(&headers, "application/msgpack") {
        rmp_serde::from_slice(&body).map_err(|_| ApiError::InvalidRequest)?
    } else {
        serde_json::from_slice(&body).map_err(|_| ApiError::InvalidRequest)?
    };
    if input
        .reservation
        .as_ref()
        .is_some_and(|reference| reference.model != state.metadata.model.model)
    {
        return Err(ApiError::InvalidRequest);
    }
    if !state.health.healthy() {
        return Err(ApiError::Unavailable);
    }
    let permit = state.health.try_admit().map_err(|error| match error {
        AdmissionRejection::Closed => ApiError::Unavailable,
        AdmissionRejection::Busy => ApiError::Busy,
    })?;
    let request_id = input.request_id.clone();
    let stream = start_execution(state, input, permit).await?;
    // Once headers are sent, backend failures become typed terminal events rather than a new HTTP status.
    let body_stream = stream.map(move |item| {
        let event = match item {
            Ok(event) => event,
            Err(error) => TokenEvent::Error {
                request_id: request_id.clone(),
                code: error.token_error_code(),
            },
        };
        let mut encoded = serde_json::to_vec(&event).expect("TokenEvent always serializes");
        encoded.push(b'\n');
        Ok::<Bytes, Infallible>(Bytes::from(encoded))
    });
    Ok((
        StatusCode::OK,
        [(header::CONTENT_TYPE, "application/x-ndjson")],
        Body::from_stream(body_stream),
    )
        .into_response())
}

// Submission outlives the HTTP handler. A disconnect during engine submission must not drop
// the future and leave an accepted request without a completion owner.
async fn start_execution(
    state: AppState,
    mut input: GenerateInput,
    permit: AdmissionPermit,
) -> Result<crate::backend::TokenStream, ApiError> {
    let (reply, received) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let owner = uuid::Uuid::new_v4().to_string();
        let shared = match input.reservation.take() {
            Some(reference) => match state.ledger.clone() {
                Some(store) => Some((store, reference)),
                None => {
                    let _ = reply.send(Err(ApiError::Unavailable));
                    return;
                }
            },
            None => None,
        };
        if let Some((store, reference)) = &shared {
            loop {
                match store
                    .ledger
                    .accept(reference, &owner, &store.pod_uid, store.epoch)
                    .await
                {
                    Ok(foretoken_request_ledger::Outcome::Applied) => break,
                    Ok(foretoken_request_ledger::Outcome::BackendUnavailable) => {
                        let _ = reply.send(Err(ApiError::AdmissionUnavailable));
                        return;
                    }
                    Ok(_) => {
                        drop(permit);
                        finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
                        let _ = reply.send(Err(ApiError::InvalidRequest));
                        return;
                    }
                    Err(_) if reply.is_closed() => {
                        drop(permit);
                        finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
                        return;
                    }
                    Err(_) => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
                }
            }
        }
        if reply.is_closed() {
            drop(permit);
            finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
            return;
        }
        let request_id = input.request_id.clone();
        let submission = tokio::select! {
            biased;
            () = state.health.wait_stopped() => Err(BackendError::Unavailable),
            result = state.backend.generate(input) => result,
        };
        let generation = match submission {
            Ok(generation) => generation,
            Err(error) => {
                let _ = reply.send(Err(ApiError::backend(error)));
                let end = if matches!(error, BackendError::InvalidRequest | BackendError::Rejected)
                {
                    ReservationEnd::NotSubmitted
                } else {
                    stop_uncertain_execution(&state.health).await;
                    ReservationEnd::RequestTerminated
                };
                drop(permit);
                finish_shared(&shared, &owner, end).await;
                return;
            }
        };
        let cancel = Arc::new(tokio::sync::Notify::new());
        let cancellation = cancel.clone();
        let (completed, mut completion) = tokio::sync::watch::channel(false);
        let crate::backend::BackendGeneration {
            mut stream,
            completion: mut engine_completion,
        } = generation;
        tokio::spawn(async move {
            let cancellation_requested = async {
                loop {
                    tokio::select! {
                        _ = cancellation.notified() => return,
                        _ = tokio::time::sleep(std::time::Duration::from_millis(250)) => {},
                    }
                    if let Some((store, reference)) = &shared
                        && matches!(store.ledger.cancelled(reference).await, Ok(true))
                    {
                        return;
                    }
                }
            };
            let result = tokio::select! {
                result = &mut engine_completion => result,
                () = state.health.wait_stopped() => Err(BackendError::Unavailable),
                () = cancellation_requested => {
                    let ids = [request_id];
                    let abort = tokio::select! {
                        result = state.backend.abort(&ids) => result,
                        () = state.health.wait_stopped() => Err(BackendError::Unavailable),
                    };
                    if abort.is_err() { state.health.fail_execution(); }
                    tokio::select! {
                        result = engine_completion => result,
                        () = state.health.wait_stopped() => Err(BackendError::Unavailable),
                    }
                },
            };
            if result.is_err() {
                stop_uncertain_execution(&state.health).await;
            }
            drop(permit);
            // Final output can finish while persistence recovers. Intermediate output waits for
            // the ownership handoff, because the next engine must claim the same caller slot.
            let handoff = result.is_ok()
                && shared
                    .as_ref()
                    .is_some_and(|(_, reference)| !reference.final_stage);
            if !handoff {
                completed.send_replace(true);
            }
            let end = if result.is_ok() {
                ReservationEnd::StageTerminated
            } else {
                ReservationEnd::RequestTerminated
            };
            finish_shared(&shared, &owner, end).await;
            if handoff {
                completed.send_replace(true);
            }
        });
        let cleanup = CancelExecution(cancel);
        let output: crate::backend::TokenStream = Box::pin(async_stream::stream! {
            let _cleanup = cleanup;
            while let Some(item) = stream.next().await {
                let terminal = match &item {
                    Ok(TokenEvent::Token(output)) => output.finish_reason.is_some(),
                    Ok(TokenEvent::Error { .. }) | Err(_) => true,
                };
                if terminal && completion.wait_for(|complete| *complete).await.is_err() {
                    yield Err(BackendError::Unavailable);
                    return;
                }
                yield item;
                if terminal { return; }
            }
        });
        let _ = reply.send(Ok(output));
    });
    received.await.map_err(|_| ApiError::RequestFailed)?
}

struct CancelExecution(Arc<tokio::sync::Notify>);
impl Drop for CancelExecution {
    fn drop(&mut self) {
        self.0.notify_one();
    }
}

// The managed process owner, not an HTTP EOF or abort receipt, establishes termination when
// transport failure made per-request confirmation impossible.
async fn stop_uncertain_execution(health: &RuntimeHealth) {
    health.fail_execution();
    health.wait_stopped().await;
}

#[derive(Clone, Copy)]
enum ReservationEnd {
    NotSubmitted,
    StageTerminated,
    RequestTerminated,
}

// Persist the execution owner's terminal evidence, retaining the reservation across storage outages.
async fn finish_shared(
    shared: &Option<(ExecutionStore, foretoken_request_ledger::ReservationRef)>,
    owner: &str,
    end: ReservationEnd,
) {
    let Some((store, reference)) = shared else {
        return;
    };
    let mut reference = reference.clone();
    reference.final_stage |= matches!(end, ReservationEnd::RequestTerminated);
    loop {
        let result = match end {
            ReservationEnd::NotSubmitted => {
                store
                    .ledger
                    .reject_before_submission(&reference, owner)
                    .await
            }
            ReservationEnd::StageTerminated | ReservationEnd::RequestTerminated => {
                store.ledger.complete(&reference, owner).await
            }
        };
        match result {
            Ok(()) => return,
            Err(_) => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
        }
    }
}

async fn abort(
    State(state): State<AppState>,
    input: Result<Json<AbortInput>, JsonRejection>,
) -> Result<StatusCode, ApiError> {
    let Json(input) = input.map_err(|_| ApiError::InvalidRequest)?;
    if !state.health.healthy() {
        return Err(ApiError::Unavailable);
    }
    if input.request_ids.is_empty() {
        return Err(ApiError::InvalidRequest);
    }
    state
        .backend
        .abort(&input.request_ids)
        .await
        .map_err(ApiError::backend)?;
    Ok(StatusCode::NO_CONTENT)
}

// Translate one frontend cursor into a bounded rank-local delta response. The adapter retains
// cursor history; this handler only publishes a cloned wire response or its reset signal.
async fn kv_index_delta(
    State(state): State<AppState>,
    Query(query): Query<KvDeltaQuery>,
) -> Response {
    let Some(adapter) = state.kv_events else {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    };
    match adapter.delta(
        query.dp_rank,
        query.epoch.as_deref(),
        query.after,
        query.limit.unwrap_or(256),
    ) {
        Ok(delta) => (StatusCode::OK, Json(delta)).into_response(),
        Err(KvDeltaError::Unavailable) => StatusCode::SERVICE_UNAVAILABLE.into_response(),
        Err(KvDeltaError::CursorReset(clear)) => {
            (StatusCode::CONFLICT, Json(clear)).into_response()
        }
    }
}
fn content_type_is(headers: &HeaderMap, expected: &str) -> bool {
    headers
        .get(header::CONTENT_TYPE)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.split(';').next())
        .is_some_and(|value| value.trim().eq_ignore_ascii_case(expected))
}

fn status(healthy: bool) -> StatusCode {
    if healthy {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

/// Fixed, wire-safe errors for the internal API.
#[derive(Debug, Clone, Copy)]
enum ApiError {
    InvalidRequest,
    Unavailable,
    Busy,
    AdmissionUnavailable,
    Rejected,
    Protocol,
    RequestFailed,
}
impl ApiError {
    fn backend(error: BackendError) -> Self {
        match error {
            BackendError::InvalidRequest => Self::InvalidRequest,
            BackendError::Unavailable => Self::Unavailable,
            BackendError::Rejected => Self::Rejected,
            BackendError::Protocol => Self::Protocol,
            BackendError::RequestFailed => Self::RequestFailed,
        }
    }
    const fn status(self) -> StatusCode {
        match self {
            Self::InvalidRequest => StatusCode::BAD_REQUEST,
            Self::Unavailable | Self::Busy | Self::AdmissionUnavailable => {
                StatusCode::SERVICE_UNAVAILABLE
            }
            Self::Rejected | Self::Protocol | Self::RequestFailed => StatusCode::BAD_GATEWAY,
        }
    }
    const fn code(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid_request",
            Self::Unavailable => "unavailable",
            Self::Busy => "admission_busy",
            Self::AdmissionUnavailable => "admission_unavailable",
            Self::Rejected => "rejected",
            Self::Protocol => "protocol",
            Self::RequestFailed => "request_failed",
        }
    }
    const fn message(self) -> &'static str {
        match self {
            Self::InvalidRequest => "invalid internal request",
            Self::Unavailable => "model server is unavailable",
            Self::Busy => "model server acceptance capacity is full",
            Self::AdmissionUnavailable => "model server admission membership is not published",
            Self::Rejected => "model server rejected the request",
            Self::Protocol => "model server protocol failed",
            Self::RequestFailed => "model server request failed",
        }
    }
}
#[derive(Serialize)]
struct ErrorBody {
    error: ErrorDetail,
}
#[derive(Serialize)]
struct ErrorDetail {
    code: &'static str,
    message: &'static str,
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (
            self.status(),
            Json(ErrorBody {
                error: ErrorDetail {
                    code: self.code(),
                    message: self.message(),
                },
            }),
        )
            .into_response()
    }
}
