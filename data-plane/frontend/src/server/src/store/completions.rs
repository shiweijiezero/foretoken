// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Completed Chat Completions and their original input messages in the shared API cache.

use serde_json::{Value, json};

use super::{
    ApiStore, Caller, Order, PageQuery, StoreError, cache_items, decode_items, decode_json,
    list_page, page_input,
};

impl ApiStore {
    /// Stores the exact final completion and input messages before publishing HTTP completion.
    /// Ordering follows public creation time; retention starts when the archive is written.
    pub(crate) async fn chat_save(
        &self,
        caller: &Caller,
        body: &Value,
        messages: &[Value],
    ) -> Result<(), StoreError> {
        let id = body
            .get("id")
            .and_then(Value::as_str)
            .ok_or(StoreError::Corrupt)?;
        let model = body
            .get("model")
            .and_then(Value::as_str)
            .ok_or(StoreError::Corrupt)?;
        let created_at = body
            .get("created")
            .and_then(Value::as_u64)
            .ok_or(StoreError::Corrupt)?;
        self.transition(
            "chat_save",
            caller,
            json!({
                "id":id,
                "body":body.to_string(),
                "model":model,
                "created_at":created_at,
                "items":cache_items(messages)?,
                "retention":self.retention_seconds,
            }),
        )
        .await
    }

    /// Lists a retained completion page within the caller's model and metadata filter scope.
    pub(crate) async fn chat_list(
        &self,
        caller: &Caller,
        query: &PageQuery,
        model: Option<&str>,
        metadata: Option<&Value>,
    ) -> Result<Value, StoreError> {
        let (limit, mut input) = page_input("", query, Order::Asc)?;
        input["model"] = json!(model);
        input["metadata"] = json!(metadata);
        let bodies: Vec<String> = self.transition("chat_list", caller, input).await?;
        let data = bodies
            .into_iter()
            .map(decode_json)
            .collect::<Result<_, _>>()?;
        Ok(list_page(data, limit))
    }

    /// Retrieves the exact retained completion without extending its expiry.
    pub(crate) async fn chat_get(&self, caller: &Caller, id: &str) -> Result<Value, StoreError> {
        let body = self
            .transition("chat_get", caller, json!({"id":id}))
            .await?;
        decode_json(body)
    }

    /// Lists a bounded page of original structured inputs in submitted order by default.
    pub(crate) async fn chat_messages(
        &self,
        caller: &Caller,
        id: &str,
        query: &PageQuery,
    ) -> Result<Value, StoreError> {
        let (limit, input) = page_input(id, query, Order::Asc)?;
        let items = self.transition("chat_messages", caller, input).await?;
        Ok(list_page(decode_items(items)?, limit))
    }

    /// Replaces metadata using compare-and-set while preserving every other public response field.
    pub(crate) async fn chat_update(
        &self,
        caller: &Caller,
        id: &str,
        metadata: &Value,
    ) -> Result<Value, StoreError> {
        loop {
            let previous_body: String = self
                .transition("chat_get", caller, json!({"id":id}))
                .await?;
            let mut body = decode_json(previous_body.clone())?;
            body.as_object_mut()
                .ok_or(StoreError::Corrupt)?
                .insert("metadata".into(), metadata.clone());
            let updated: Result<String, StoreError> = self
                .transition(
                    "chat_update",
                    caller,
                    json!({"id":id,"previous_body":previous_body,"body":body.to_string()}),
                )
                .await;
            match updated {
                Ok(body) => return decode_json(body),
                Err(StoreError::Changed) => continue,
                Err(error) => return Err(error),
            }
        }
    }

    /// Deletes the archive and releases only its ownership of shared input items.
    pub(crate) async fn chat_delete(&self, caller: &Caller, id: &str) -> Result<Value, StoreError> {
        self.transition::<()>("chat_delete", caller, json!({"id":id}))
            .await?;
        Ok(json!({"id":id,"object":"chat.completion.deleted","deleted":true}))
    }
}
