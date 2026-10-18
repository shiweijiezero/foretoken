// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Caller-scoped conversation metadata and item membership in the shared API cache.

use serde_json::{Value, json};
use uuid::Uuid;

use super::{
    ApiStore, Caller, Order, PageQuery, StoreError, cache_items, decode_items, decode_json,
    list_page, page_input,
};

impl ApiStore {
    /// Creates a conversation and its validated items in one cache transition with a retention period.
    pub(crate) async fn conversation_create(
        &self,
        caller: &Caller,
        metadata: &Value,
        items: Vec<Value>,
    ) -> Result<Value, StoreError> {
        let stored = self
            .transition(
                "conversation_create",
                caller,
                json!({
                    "id":format!("conv_{}", Uuid::new_v4().simple()),
                    "metadata":metadata.to_string(),
                    "items":cache_items(&items)?,
                    "retention":self.retention_seconds,
                }),
            )
            .await?;
        conversation_body(stored)
    }

    /// Retrieves a retained conversation visible to the ingress caller without extending its expiry.
    pub(crate) async fn conversation_get(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<Value, StoreError> {
        let stored = self
            .transition("conversation_get", caller, json!({"id":id}))
            .await?;
        conversation_body(stored)
    }

    /// Replaces metadata and refreshes retention without changing active response ownership.
    pub(crate) async fn conversation_update(
        &self,
        caller: &Caller,
        id: &str,
        metadata: &Value,
    ) -> Result<Value, StoreError> {
        let stored = self
            .transition(
                "conversation_update",
                caller,
                json!({"id":id,"metadata":metadata.to_string(),"retention":self.retention_seconds}),
            )
            .await?;
        conversation_body(stored)
    }

    /// Deletes the container while its items remain until their existing retention period ends.
    pub(crate) async fn conversation_delete(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<Value, StoreError> {
        self.transition::<()>("conversation_delete", caller, json!({"id":id}))
            .await?;
        Ok(json!({"id":id,"object":"conversation.deleted","deleted":true}))
    }

    /// Lists one bounded membership page, newest first unless explicitly ordered.
    pub(crate) async fn conversation_items(
        &self,
        caller: &Caller,
        id: &str,
        query: &PageQuery,
    ) -> Result<Value, StoreError> {
        let (limit, input) = page_input(id, query, Order::Desc)?;
        let items = self.transition("conversation_items", caller, input).await?;
        Ok(list_page(decode_items(items)?, limit))
    }

    /// Atomically appends validated items and refreshes retention while no response owns the next turn.
    pub(crate) async fn conversation_append(
        &self,
        caller: &Caller,
        id: &str,
        items: Vec<Value>,
    ) -> Result<Value, StoreError> {
        let stored = self
            .transition(
                "conversation_append",
                caller,
                json!({"id":id,"items":cache_items(&items)?,"retention":self.retention_seconds}),
            )
            .await?;
        let items = decode_items(stored)?;
        let count = items.len();
        Ok(list_page(items, count))
    }

    /// Retrieves an item through caller-scoped membership, restoring any repeated-reference alias.
    pub(crate) async fn conversation_get_item(
        &self,
        caller: &Caller,
        id: &str,
        item: &str,
    ) -> Result<Value, StoreError> {
        let raw = self
            .transition(
                "conversation_get_item",
                caller,
                json!({"id":id,"item":item}),
            )
            .await?;
        let mut body = decode_json(raw)?;
        body.as_object_mut()
            .ok_or(StoreError::Corrupt)?
            .insert("id".into(), json!(item));
        Ok(body)
    }

    /// Removes membership and refreshes retention without deleting items owned by other history views.
    pub(crate) async fn conversation_delete_item(
        &self,
        caller: &Caller,
        id: &str,
        item: &str,
    ) -> Result<Value, StoreError> {
        let stored = self
            .transition(
                "conversation_delete_item",
                caller,
                json!({"id":id,"item":item,"retention":self.retention_seconds}),
            )
            .await?;
        conversation_body(stored)
    }
}

/// Encodes public conversation fields while preserving metadata JSON without exposing cache state.
fn conversation_body(
    (id, metadata, created_at): (String, String, u64),
) -> Result<Value, StoreError> {
    Ok(json!({
        "id":id,
        "object":"conversation",
        "created_at":created_at,
        "metadata":decode_json(metadata)?,
    }))
}
