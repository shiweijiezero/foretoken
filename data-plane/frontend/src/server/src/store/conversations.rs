// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Conversation metadata and ordered membership of immutable API items.

use serde_json::{Value, json};
use sqlx::postgres::PgRow;
use sqlx::{Postgres, Row, Transaction};
use uuid::Uuid;

use super::{ApiStore, Caller, Order, PageQuery, StoreError, list_page};

impl ApiStore {
    /// Creates a caller-scoped conversation and its prepared items in one transaction.
    pub(crate) async fn conversation_create(
        &self,
        caller: &Caller,
        metadata: &Value,
        mut items: Vec<Value>,
    ) -> Result<Value, StoreError> {
        let id = format!("conv_{}", Uuid::new_v4().simple());
        let mut transaction = self.pool().await?.begin().await?;
        let row = sqlx::query(
            "INSERT INTO foretoken_api_conversations (namespace,id,caller,metadata) \
             VALUES ($1,$2,$3,$4) RETURNING id,metadata, \
             FLOOR(EXTRACT(EPOCH FROM created_at))::BIGINT AS created_at",
        )
        .bind(&self.namespace)
        .bind(&id)
        .bind(caller)
        .bind(metadata)
        .fetch_one(&mut *transaction)
        .await?;
        self.insert_conversation_items(&mut transaction, &id, caller, &mut items)
            .await?;
        let conversation = conversation_body(&row)?;
        transaction.commit().await?;
        Ok(conversation)
    }

    /// Retrieves an undeleted conversation visible to the ingress caller.
    pub(crate) async fn conversation_get(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<Value, StoreError> {
        let row = sqlx::query(
            "SELECT id,metadata,FLOOR(EXTRACT(EPOCH FROM created_at))::BIGINT AS created_at \
             FROM foretoken_api_conversations \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND NOT deleted",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .fetch_optional(self.pool().await?)
        .await?
        .ok_or(StoreError::NotFound)?;
        conversation_body(&row)
    }

    /// Replaces conversation metadata without changing an active response's ownership.
    pub(crate) async fn conversation_update(
        &self,
        caller: &Caller,
        id: &str,
        metadata: &Value,
    ) -> Result<Value, StoreError> {
        let row = sqlx::query(
            "UPDATE foretoken_api_conversations SET metadata=$4 \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND NOT deleted \
             RETURNING id,metadata,FLOOR(EXTRACT(EPOCH FROM created_at))::BIGINT AS created_at",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .bind(metadata)
        .fetch_optional(self.pool().await?)
        .await?
        .ok_or(StoreError::NotFound)?;
        conversation_body(&row)
    }

    /// Deletes the conversation container while preserving its items and any response ownership.
    pub(crate) async fn conversation_delete(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<Value, StoreError> {
        let deleted = sqlx::query(
            "UPDATE foretoken_api_conversations SET deleted=TRUE \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND NOT deleted",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .execute(self.pool().await?)
        .await?;
        if deleted.rows_affected() == 0 {
            return Err(StoreError::NotFound);
        }
        Ok(json!({"id":id,"object":"conversation.deleted","deleted":true}))
    }

    /// Lists a bounded page of current membership, newest first unless explicitly ordered.
    pub(crate) async fn conversation_items(
        &self,
        caller: &Caller,
        id: &str,
        query: &PageQuery,
    ) -> Result<Value, StoreError> {
        let (limit, order) = query.bounds(Order::Desc)?;
        let direction = match order {
            Order::Asc => "ASC",
            Order::Desc => "DESC",
        };
        let comparison = if matches!(order, Order::Asc) == query.after.is_some() {
            ">"
        } else {
            "<"
        };
        let mut transaction = self.pool().await?.begin().await?;
        sqlx::query(
            "SELECT id FROM foretoken_api_conversations \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND NOT deleted \
             FOR SHARE",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or(StoreError::NotFound)?;

        // Keep membership stable while resolving the cursor and reading its bounded page.
        let cursor = if let Some(item) = query.after.as_deref().or(query.before.as_deref()) {
            let sql = format!(
                "SELECT position FROM foretoken_api_conversation_items \
                 WHERE namespace=$1 AND conversation_id=$2 AND item_id=$3 \
                 ORDER BY position {direction} LIMIT 1"
            );
            Some(
                sqlx::query_scalar::<_, i64>(&sql)
                    .bind(&self.namespace)
                    .bind(id)
                    .bind(item)
                    .fetch_optional(&mut *transaction)
                    .await?
                    .ok_or_else(|| {
                        StoreError::Invalid("the pagination cursor was not found".into())
                    })?,
            )
        } else {
            None
        };
        let sql = format!(
            "SELECT i.body FROM foretoken_api_conversation_items c \
             JOIN foretoken_api_items i ON i.namespace=c.namespace AND i.id=c.item_id \
             WHERE c.namespace=$1 AND c.conversation_id=$2 \
             AND ($3::BIGINT IS NULL OR c.position {comparison} $3) \
             ORDER BY c.position {direction} LIMIT $4"
        );
        let items = sqlx::query_scalar(&sql)
            .bind(&self.namespace)
            .bind(id)
            .bind(cursor)
            .bind((limit + 1) as i64)
            .fetch_all(&mut *transaction)
            .await?;
        transaction.commit().await?;
        Ok(list_page(items, limit))
    }

    /// Appends prepared items only while no response owns the conversation's next turn.
    pub(crate) async fn conversation_append(
        &self,
        caller: &Caller,
        id: &str,
        mut items: Vec<Value>,
    ) -> Result<Value, StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let row = self
            .conversation_for_mutation(&mut transaction, caller, id)
            .await?;
        if row
            .try_get::<Option<String>, _>("active_response")?
            .is_some()
        {
            return Err(StoreError::ConversationBusy);
        }
        self.insert_conversation_items(&mut transaction, id, caller, &mut items)
            .await?;
        transaction.commit().await?;
        let count = items.len();
        Ok(list_page(items, count))
    }

    /// Retrieves an item only while it belongs to a visible conversation.
    pub(crate) async fn conversation_get_item(
        &self,
        caller: &Caller,
        id: &str,
        item: &str,
    ) -> Result<Value, StoreError> {
        sqlx::query_scalar(
            "SELECT i.body FROM foretoken_api_conversations c \
             JOIN foretoken_api_conversation_items m \
             ON m.namespace=c.namespace AND m.conversation_id=c.id \
             JOIN foretoken_api_items i ON i.namespace=m.namespace AND i.id=m.item_id \
             WHERE c.namespace=$1 AND c.id=$2 AND c.caller IS NOT DISTINCT FROM $3 \
             AND NOT c.deleted AND m.item_id=$4 LIMIT 1",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .bind(item)
        .fetch_optional(self.pool().await?)
        .await?
        .ok_or(StoreError::NotFound)
    }

    /// Removes item membership without destroying immutable history used by response branches.
    pub(crate) async fn conversation_delete_item(
        &self,
        caller: &Caller,
        id: &str,
        item: &str,
    ) -> Result<Value, StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let row = self
            .conversation_for_mutation(&mut transaction, caller, id)
            .await?;
        if row
            .try_get::<Option<String>, _>("active_response")?
            .is_some()
        {
            return Err(StoreError::ConversationBusy);
        }
        let deleted = sqlx::query(
            "DELETE FROM foretoken_api_conversation_items \
             WHERE namespace=$1 AND conversation_id=$2 AND item_id=$3",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(item)
        .execute(&mut *transaction)
        .await?;
        if deleted.rows_affected() == 0 {
            return Err(StoreError::NotFound);
        }
        let conversation = conversation_body(&row)?;
        transaction.commit().await?;
        Ok(conversation)
    }

    /// Serializes client membership changes with Responses begin and finish transactions.
    async fn conversation_for_mutation(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
        caller: &Caller,
        id: &str,
    ) -> Result<PgRow, StoreError> {
        sqlx::query(
            "SELECT id,metadata,active_response, \
             FLOOR(EXTRACT(EPOCH FROM created_at))::BIGINT AS created_at \
             FROM foretoken_api_conversations \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND NOT deleted \
             FOR UPDATE",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .fetch_optional(&mut **transaction)
        .await?
        .ok_or(StoreError::NotFound)
    }

    /// Adds items under an already locked conversation for client appends and Responses completion.
    /// Repeated references receive new IDs in place; content, order, and existing items stay unchanged.
    pub(super) async fn insert_conversation_items(
        &self,
        transaction: &mut Transaction<'_, Postgres>,
        conversation: &str,
        caller: &Caller,
        items: &mut [Value],
    ) -> Result<(), StoreError> {
        let next_position: i64 = sqlx::query_scalar(
            "SELECT COALESCE(MAX(position)+1,0) FROM foretoken_api_conversation_items \
             WHERE namespace=$1 AND conversation_id=$2",
        )
        .bind(&self.namespace)
        .bind(conversation)
        .fetch_one(&mut **transaction)
        .await?;
        for (offset, item) in items.iter_mut().enumerate() {
            let mut id = item
                .get("id")
                .and_then(Value::as_str)
                .ok_or(StoreError::Corrupt)?
                .to_owned();
            let duplicate: bool = sqlx::query_scalar(
                "SELECT EXISTS (SELECT 1 FROM foretoken_api_conversation_items \
                 WHERE namespace=$1 AND conversation_id=$2 AND item_id=$3)",
            )
            .bind(&self.namespace)
            .bind(conversation)
            .bind(&id)
            .fetch_one(&mut **transaction)
            .await?;
            if duplicate {
                id = format!("item_{}", Uuid::new_v4().simple());
                item["id"] = Value::String(id.clone());
            }
            sqlx::query(
                "INSERT INTO foretoken_api_items (namespace,id,caller,body) VALUES ($1,$2,$3,$4) \
                 ON CONFLICT (namespace,id) DO NOTHING",
            )
            .bind(&self.namespace)
            .bind(&id)
            .bind(caller)
            .bind(&*item)
            .execute(&mut **transaction)
            .await?;
            sqlx::query(
                "INSERT INTO foretoken_api_conversation_items \
                 (namespace,conversation_id,item_id,position) VALUES ($1,$2,$3,$4)",
            )
            .bind(&self.namespace)
            .bind(conversation)
            .bind(&id)
            .bind(next_position + offset as i64)
            .execute(&mut **transaction)
            .await?;
        }
        Ok(())
    }
}

/// Encodes public conversation fields without exposing execution ownership or tombstones.
fn conversation_body(row: &PgRow) -> Result<Value, StoreError> {
    Ok(json!({
        "id":row.try_get::<String,_>("id")?,
        "object":"conversation",
        "created_at":row.try_get::<i64,_>("created_at")?,
        "metadata":row.try_get::<Value,_>("metadata")?,
    }))
}
