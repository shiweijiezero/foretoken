// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Prefix observations and source-lease control on the execution group's internal listener.

use axum::Json;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use foretoken_model_protocol::{
    KV_OBSERVATION_TIMEOUT, KvDeltaQuery, KvPrefixCancel, KvPrefixCancelResult, KvPrefixClaim,
    KvPrefixClaimResult, KvPrefixRequest,
};

use super::{ApiError, AppState};
use crate::backend::KvDeltaError;

/// Query schedulers without admitting a generation or acquiring source blocks.
pub(super) async fn local_kv_prefix(
    State(state): State<AppState>,
    Json(request): Json<KvPrefixRequest>,
) -> Response {
    if !state.health.ready() || !state.health.accepting() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    match tokio::time::timeout(
        KV_OBSERVATION_TIMEOUT,
        state.backend.local_prefixes(&request),
    )
    .await
    {
        Ok(Ok(response)) => Json(response).into_response(),
        _ => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
}

/// Observe shared prefixes only while engine health and admission permit reads.
pub(super) async fn shared_kv_prefix(
    State(state): State<AppState>,
    Json(request): Json<KvPrefixRequest>,
) -> Response {
    if !state.health.ready() || !state.health.accepting() {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    match tokio::time::timeout(
        KV_OBSERVATION_TIMEOUT,
        state.backend.shared_prefix(&request),
    )
    .await
    {
        Ok(Ok(response)) => Json(response).into_response(),
        _ => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
}

/// Publish retained prefixes and their physical source endpoint without acquiring native references.
pub(super) async fn retained_kv_prefix(
    State(state): State<AppState>,
    Json(request): Json<KvPrefixRequest>,
) -> Response {
    if !state.health.ready()
        || !state.health.accepting()
        || !state
            .metadata
            .kv_transfer()
            .is_some_and(|kv| kv.bidirectional_kv_xfer)
    {
        return StatusCode::SERVICE_UNAVAILABLE.into_response();
    }
    match tokio::time::timeout(
        KV_OBSERVATION_TIMEOUT,
        state.backend.retained_prefixes(&request),
    )
    .await
    {
        Ok(Ok(response)) => Json(response).into_response(),
        _ => StatusCode::SERVICE_UNAVAILABLE.into_response(),
    }
}

/// Acquire a prefix only while this source instance still accepts new reads.
pub(super) async fn claim_kv_prefix(
    State(state): State<AppState>,
    Json(request): Json<KvPrefixClaim>,
) -> Response {
    if !state.health.ready()
        || !state.health.accepting()
        || !state
            .metadata
            .kv_transfer()
            .is_some_and(|kv| kv.bidirectional_kv_xfer)
    {
        return Json(KvPrefixClaimResult::Miss).into_response();
    }
    match state.backend.claim_kv_prefix(&request).await {
        Ok(result) => Json(result).into_response(),
        Err(error) => ApiError::backend(error).into_response(),
    }
}

/// Cancel an existing claim during drain; native completion determines when its blocks can be freed.
pub(super) async fn cancel_kv_prefix(
    State(state): State<AppState>,
    Json(request): Json<KvPrefixCancel>,
) -> Response {
    if !state
        .metadata
        .kv_transfer()
        .is_some_and(|kv| kv.bidirectional_kv_xfer)
        || !state.health.process_alive()
    {
        return Json(KvPrefixCancelResult::Miss).into_response();
    }
    match state.backend.cancel_kv_prefix(&request).await {
        Ok(result) => Json(result).into_response(),
        Err(error) => ApiError::backend(error).into_response(),
    }
}

/// Translate a frontend cursor into a bounded delta or reset signal; the backend owns cursor history.
pub(super) async fn kv_index_delta(
    State(state): State<AppState>,
    Query(query): Query<KvDeltaQuery>,
) -> Response {
    match state.backend.kv_delta(
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
