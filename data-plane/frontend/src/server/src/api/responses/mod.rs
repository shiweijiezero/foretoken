// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the vLLM project
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Responses protocol adapters over the shared execution lifecycle and API state store.

mod convert;
pub(super) mod error;
mod execution;
mod streaming;
mod tools;
mod types;

pub use execution::ResponseTasks;

use std::convert::Infallible;
use std::sync::Arc;
use std::time::Duration;

use axum::extract::rejection::JsonRejection;
use axum::extract::{Path, Query, State};
use axum::response::sse::Event;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use foretoken_admission::CallerIdentity;
use serde::Deserialize;
use serde_json::{Value, json};
use tokio::sync::mpsc;

use crate::api::stream::sse_response;
use crate::api::{ApiState, RequestTiming};
use crate::runtime::{GenerationError, RequestSnapshot};
use crate::store::{ApiStore, Caller, PageQuery, StoreError};
use error::ApiError;
use execution::{Disconnect, ResponseReady};
use types::ResponsesRequest;

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
struct RetrieveQuery {
    #[serde(default)]
    stream: bool,
    starting_after: Option<i64>,
}

/// Registers creation, stored response access, cancellation, input history and replay.
pub(super) fn router() -> Router<ApiState> {
    Router::new()
        .route("/v1/responses", post(create))
        .route("/v1/responses/{id}", get(retrieve).delete(delete))
        .route("/v1/responses/{id}/cancel", post(cancel))
        .route("/v1/responses/{id}/input_items", get(input_items))
}

/// Applies the same item conversion rules to independently managed conversation items.
pub(crate) fn validate_stored_items(items: &[Value]) -> Result<(), String> {
    convert::convert_input(&None, types::ResponsesInput::Items(items.to_vec()))
        .map(|_| ())
        .map_err(|error| {
            error.body["message"]
                .as_str()
                .unwrap_or("Invalid conversation items")
                .to_owned()
        })
}

async fn create(
    State(api): State<ApiState>,
    Extension(snapshot): Extension<RequestSnapshot>,
    timing: Option<Extension<RequestTiming>>,
    body: Result<Json<ResponsesRequest>, JsonRejection>,
) -> Response {
    let timing = timing.map_or_else(RequestTiming::now, |Extension(timing)| timing);
    let request = match body {
        Ok(Json(request)) => request,
        Err(error) => {
            let mut response = ApiError::invalid_request("Invalid Responses request body", None);
            if error.status() == axum::http::StatusCode::PAYLOAD_TOO_LARGE {
                response.status = axum::http::StatusCode::PAYLOAD_TOO_LARGE;
                response.body["message"] = json!("Request body exceeds the service limit");
            }
            return response.into_response();
        }
    };
    let streamed = request.stream;
    let caller = CallerIdentity::current().caller;
    let delivery = api.response_tasks.start(snapshot, request, timing);
    match delivery.ready.await {
        Ok(Ok(ResponseReady::Stored(body))) if streamed => {
            let id = body["id"].as_str().expect("response identity").to_owned();
            let store = api.store.expect("background response has storage");
            sse_response(replay(store, caller, id, -1))
        }
        Ok(Ok(ResponseReady::Stored(body) | ResponseReady::Collected(body))) => {
            Json(body).into_response()
        }
        Ok(Ok(ResponseReady::Streaming)) => {
            sse_response(live_stream(delivery.events, delivery.disconnect))
        }
        Ok(Err(error)) => error.into_response(),
        Err(_) => ApiError::generation(GenerationError::RequestFailed).into_response(),
    }
}

async fn retrieve(
    State(api): State<ApiState>,
    Path(id): Path<String>,
    Query(query): Query<RetrieveQuery>,
) -> Response {
    let Some(store) = api.store else {
        return ApiError::from(StoreError::Disabled).into_response();
    };
    let caller = CallerIdentity::current().caller;
    let response = match store.response(&caller, &id).await {
        Ok(response) => response,
        Err(error) => return ApiError::from(error).into_response(),
    };
    if !query.stream {
        if query.starting_after.is_some() {
            return ApiError::invalid_request(
                "starting_after requires stream=true",
                Some("starting_after"),
            )
            .into_response();
        }
        return Json(response.body).into_response();
    }
    if !response.background || !response.streamed {
        return ApiError::invalid_request(
            "stream replay requires a response created with background=true and stream=true",
            Some("stream"),
        )
        .into_response();
    }
    if response.replay_expired {
        return ApiError::from(StoreError::StreamExpired).into_response();
    }
    if let Some(after) = query.starting_after {
        let published = match store.has_event(&id, after).await {
            Ok(published) => published,
            Err(error) => return ApiError::from(error).into_response(),
        };
        if after < 0 || !published {
            return ApiError::invalid_request(
                "starting_after is not a published event sequence",
                Some("starting_after"),
            )
            .into_response();
        }
    }
    sse_response(replay(
        store,
        caller,
        id,
        query.starting_after.unwrap_or(-1),
    ))
}

async fn delete(State(api): State<ApiState>, Path(id): Path<String>) -> Response {
    let Some(store) = api.store else {
        return ApiError::from(StoreError::Disabled).into_response();
    };
    match store
        .delete_response(&CallerIdentity::current().caller, &id)
        .await
    {
        Ok(()) => Json(json!({"id":id,"object":"response.deleted","deleted":true})).into_response(),
        Err(error) => ApiError::from(error).into_response(),
    }
}

async fn cancel(State(api): State<ApiState>, Path(id): Path<String>) -> Response {
    let Some(store) = api.store else {
        return ApiError::from(StoreError::Disabled).into_response();
    };
    let caller = CallerIdentity::current().caller;
    match store.cancel_response(&caller, &id).await {
        Ok(body) => {
            api.response_tasks.request_cancel(&id);
            Json(body).into_response()
        }
        Err(error) => ApiError::from(error).into_response(),
    }
}

async fn input_items(
    State(api): State<ApiState>,
    Path(id): Path<String>,
    Query(page): Query<PageQuery>,
) -> Response {
    let Some(store) = api.store else {
        return ApiError::from(StoreError::Disabled).into_response();
    };
    match store
        .response_inputs(&CallerIdentity::current().caller, &id, &page)
        .await
    {
        Ok(body) => Json(body).into_response(),
        Err(error) => ApiError::from(error).into_response(),
    }
}

fn event(value: Value) -> Result<Event, Infallible> {
    let kind = value.get("type").and_then(Value::as_str).unwrap_or("error");
    Ok(Event::default()
        .event(kind)
        .json_data(&value)
        .expect("response event serialization"))
}

/// Retains foreground cancellation until the last HTTP body consumer leaves.
fn live_stream(
    mut receiver: mpsc::Receiver<Vec<Value>>,
    disconnect: Disconnect,
) -> impl futures::Stream<Item = Result<Event, Infallible>> + Send {
    async_stream::stream! {
        let _disconnect = disconnect;
        while let Some(events) = receiver.recv().await {
            for value in events { yield event(value); }
        }
    }
}

/// Replays committed events from any replica without acquiring inference capacity.
fn replay(
    store: Arc<ApiStore>,
    caller: Caller,
    id: String,
    mut after: i64,
) -> impl futures::Stream<Item = Result<Event, Infallible>> + Send {
    async_stream::stream! {
        loop {
            let response = match store.response(&caller, &id).await {
                Ok(response) if !response.replay_expired => response,
                Ok(_) => {
                    yield event(json!({"type":"error","error":{"code":"stream_expired","message":"Response stream has expired"}}));
                    return;
                }
                Err(error) => {
                    yield event(json!({"type":"error","error":ApiError::from(error).body}));
                    return;
                }
            };
            let events = match store.events(&id, after).await {
                Ok(events) => events,
                Err(error) => {
                    yield event(json!({"type":"error","error":ApiError::from(error).body}));
                    return;
                }
            };
            let had_events = !events.is_empty();
            for value in events {
                after = value["sequence_number"].as_i64().expect("stored event sequence");
                yield event(value);
            }
            if had_events { continue; }
            if !matches!(response.status.as_str(), "queued" | "in_progress") { return; }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }
}

/// Restores external tool names before either persistence or public response delivery.
fn restore_tools(response: &mut Value, names: &tools::ToolNames) -> Result<(), ApiError> {
    if let Some(items) = response.get_mut("output").and_then(Value::as_array_mut) {
        for item in items {
            names.restore_item(item)?;
        }
    }
    Ok(())
}
