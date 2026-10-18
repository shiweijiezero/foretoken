// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Conversations endpoints over caller-scoped metadata and immutable Responses items.

use axum::extract::rejection::{JsonRejection, QueryRejection};
use axum::extract::{Path, Query, State};
use axum::response::Response;
use axum::routing::{get, post};
use axum::{Json, Router};
use foretoken_admission::CallerIdentity;
use serde::Deserialize;
use serde_json::{Map, Value};

use super::openai::{client_error, json_error};
use super::{ApiState, responses, store_error};
use crate::store::{ApiStore, Caller, PageQuery, StoreError, input_items};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct CreateConversation {
    #[serde(default)]
    metadata: Map<String, Value>,
    #[serde(default)]
    items: Vec<Value>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct UpdateConversation {
    metadata: Map<String, Value>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AppendItems {
    items: Vec<Value>,
}

/// Registers resource operations independently of model readiness and inference admission.
pub(super) fn router() -> Router<ApiState> {
    Router::new()
        .route("/v1/conversations", post(create))
        .route(
            "/v1/conversations/{id}",
            get(retrieve).post(update).delete(delete),
        )
        .route(
            "/v1/conversations/{id}/items",
            get(list_items).post(append_items),
        )
        .route(
            "/v1/conversations/{id}/items/{item_id}",
            get(retrieve_item).delete(delete_item),
        )
}

/// Validates and creates a conversation before publishing its public identity.
async fn create(
    State(state): State<ApiState>,
    body: Result<Json<CreateConversation>, JsonRejection>,
) -> Result<Json<Value>, Response> {
    let Json(body) = body.map_err(json_error)?;
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    let caller = CallerIdentity::current().caller;
    let items = prepare_items(&store, &caller, body.items)
        .await
        .map_err(store_error)?;
    store
        .conversation_create(&caller, &Value::Object(body.metadata), items)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Reads conversation metadata without acquiring execution capacity.
async fn retrieve(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, Response> {
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .conversation_get(&CallerIdentity::current().caller, &id)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Replaces metadata while preserving any in-progress Responses turn.
async fn update(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    body: Result<Json<UpdateConversation>, JsonRejection>,
) -> Result<Json<Value>, Response> {
    let Json(body) = body.map_err(json_error)?;
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .conversation_update(
            &CallerIdentity::current().caller,
            &id,
            &Value::Object(body.metadata),
        )
        .await
        .map(Json)
        .map_err(store_error)
}

/// Deletes only the conversation container, leaving response history intact.
async fn delete(
    State(state): State<ApiState>,
    Path(id): Path<String>,
) -> Result<Json<Value>, Response> {
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .conversation_delete(&CallerIdentity::current().caller, &id)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Lists conversation membership with public item cursors and newest-first defaults.
async fn list_items(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    query: Result<Query<PageQuery>, QueryRejection>,
) -> Result<Json<Value>, Response> {
    let Query(query) = query.map_err(|error| client_error(error.body_text()))?;
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .conversation_items(&CallerIdentity::current().caller, &id, &query)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Validates new history before atomically appending its items and membership.
async fn append_items(
    State(state): State<ApiState>,
    Path(id): Path<String>,
    body: Result<Json<AppendItems>, JsonRejection>,
) -> Result<Json<Value>, Response> {
    let Json(body) = body.map_err(json_error)?;
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    let caller = CallerIdentity::current().caller;
    let items = prepare_items(&store, &caller, body.items)
        .await
        .map_err(store_error)?;
    store
        .conversation_append(&caller, &id, items)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Retrieves an item through its conversation membership and ingress caller scope.
async fn retrieve_item(
    State(state): State<ApiState>,
    Path((id, item)): Path<(String, String)>,
) -> Result<Json<Value>, Response> {
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .conversation_get_item(&CallerIdentity::current().caller, &id, &item)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Deletes membership and returns the remaining conversation container.
async fn delete_item(
    State(state): State<ApiState>,
    Path((id, item)): Path<(String, String)>,
) -> Result<Json<Value>, Response> {
    let store = state
        .store
        .ok_or_else(|| store_error(StoreError::Disabled))?;
    store
        .conversation_delete_item(&CallerIdentity::current().caller, &id, &item)
        .await
        .map(Json)
        .map_err(store_error)
}

/// Assigns new item IDs, resolves caller-owned references, and reuses Responses input validation.
async fn prepare_items(
    store: &ApiStore,
    caller: &Caller,
    items: Vec<Value>,
) -> Result<Vec<Value>, StoreError> {
    let items = input_items(Value::Array(items))?;
    let mut expanded = items.clone();
    for item in &mut expanded {
        if item.get("type").and_then(Value::as_str) == Some("item_reference") {
            let id = item
                .get("id")
                .and_then(Value::as_str)
                .ok_or_else(|| StoreError::Invalid("item_reference requires an id".into()))?;
            *item = store.item(caller, id).await?;
        }
    }
    responses::validate_stored_items(&expanded).map_err(StoreError::Invalid)?;
    Ok(items)
}
