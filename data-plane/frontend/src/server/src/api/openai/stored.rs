// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Stored Chat Completion endpoints and per-request archive ownership.

use std::collections::HashMap;
use std::sync::Arc;

use axum::extract::rejection::{JsonRejection, QueryRejection};
use axum::extract::{Path, Query, State};
use axum::response::Response;
use axum::routing::get;
use axum::{Json, Router};
use foretoken_admission::CallerIdentity;
use serde::Deserialize;
use serde_json::{Map, Value, json};
use uuid::Uuid;

use super::{client_error, json_error};
use crate::api::{ApiState, store_error};
use crate::store::{ApiStore, Caller, Order, PageQuery, StoreError};

/// Immutable archive context retained by the output adapter until its final transaction commits.
pub(crate) struct ChatArchive {
    store: Arc<ApiStore>,
    caller: Caller,
    original_messages: Vec<Value>,
    pub(super) metadata: Value,
}

impl ChatArchive {
    /// Captures validated original messages with new public IDs, preserving structured content.
    pub(super) fn new(
        store: Arc<ApiStore>,
        caller: Caller,
        mut original_messages: Vec<Value>,
        metadata: Value,
    ) -> Self {
        for message in &mut original_messages {
            message
                .as_object_mut()
                .expect("validated chat message object")
                .insert(
                    "id".into(),
                    json!(format!("msg_{}", Uuid::new_v4().simple())),
                );
        }
        Self {
            store,
            caller,
            original_messages,
            metadata,
        }
    }

    /// Stores the final public response atomically with its inputs before the client sees completion.
    pub(super) async fn save(&self, body: &Value) -> Result<(), StoreError> {
        self.store
            .chat_save(&self.caller, body, &self.original_messages)
            .await
    }
}

#[derive(Default, Deserialize)]
struct ListCompletions {
    after: Option<String>,
    limit: Option<usize>,
    order: Option<Order>,
    model: Option<String>,
    #[serde(flatten)]
    filters: HashMap<String, String>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UpdateCompletion {
    metadata: Map<String, Value>,
}

/// Registers archive reads and metadata updates separately from the generation handler.
pub(super) fn router() -> Router<ApiState> {
    Router::new()
        .route("/v1/chat/completions", get(list))
        .route(
            "/v1/chat/completions/{id}",
            get(retrieve).post(update).delete(delete),
        )
        .route("/v1/chat/completions/{id}/messages", get(messages))
}

/// Lists retained completions with SDK-style metadata[key]=value filters.
async fn list(
    State(state): State<ApiState>,
    query: Result<Query<ListCompletions>, QueryRejection>,
) -> Result<Json<Value>, Response> {
    let Query(query) = query.map_err(|error| client_error(error.body_text()))?;
    let mut metadata = Map::new();
    for (key, value) in query.filters {
        let name = key
            .strip_prefix("metadata[")
            .and_then(|name| name.strip_suffix(']'))
            .filter(|name| !name.is_empty())
            .ok_or_else(|| client_error(format!("unsupported query parameter: {key}")))?;
        metadata.insert(name.to_owned(), Value::String(value));
    }
    let metadata = (!metadata.is_empty()).then_some(Value::Object(metadata));
    let page = PageQuery {
        after: query.after,
        before: None,
        limit: query.limit,
        order: query.order,
    };
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .chat_list(
            &CallerIdentity::current().caller,
            &page,
            query.model.as_deref(),
            metadata.as_ref(),
        )
        .await
        .map(Json)
        .map_err(store_error)
}

/// Retrieves the same terminal public response that was committed by generation.
async fn retrieve(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, Response> {
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .chat_get(&CallerIdentity::current().caller, &id)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Lists original structured input messages in their submitted order by default.
async fn messages(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    query: Result<Query<PageQuery>, QueryRejection>,
) -> Result<Json<Value>, Response> {
    let Query(query) = query.map_err(|error| client_error(error.body_text()))?;
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .chat_messages(&CallerIdentity::current().caller, &id, &query)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Replaces only stored metadata, retaining the final generation and original messages.
async fn update(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    body: Result<Json<UpdateCompletion>, JsonRejection>,
) -> Result<Json<Value>, Response> {
    let Json(body) = body.map_err(json_error)?;
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .chat_update(
            &CallerIdentity::current().caller,
            &id,
            &Value::Object(body.metadata),
        )
        .await
        .map(Json)
        .map_err(store_error)
}

/// Deletes the caller's archive without modifying immutable items shared with other objects.
async fn delete(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, Response> {
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .chat_delete(&CallerIdentity::current().caller, &id)
        .await
        .map(Json)
        .map_err(store_error)
}
