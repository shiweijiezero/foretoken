// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Completed Chat Completions archives and their structured original input messages.

use serde_json::{Value, json};

use super::{ApiStore, Caller, Order, PageQuery, StoreError, list_page};

impl ApiStore {
    /// Commits one final public Chat Completion and its input messages before HTTP completion.
    /// Archive ordering follows public creation time; retention starts when the archive is written.
    pub(crate) async fn chat_save(
        &self,
        caller: &Caller,
        body: &Value,
        messages: &[Value],
        streamed: bool,
    ) -> Result<(), StoreError> {
        let id = body
            .get("id")
            .and_then(Value::as_str)
            .ok_or(StoreError::Corrupt)?;
        let model = body
            .get("model")
            .and_then(Value::as_str)
            .ok_or(StoreError::Corrupt)?;
        let mut transaction = self.pool().await?.begin().await?;
        sqlx::query(
            "INSERT INTO foretoken_api_records \
             (namespace,id,caller,kind,model,body,status,stored,background,streamed, \
              execution_pod,execution_instance,created_at,expires_at) \
             VALUES ($1,$2,$3,'chat',$4,$5,'completed',TRUE,FALSE,$6,$7,$8, \
                     TIMESTAMPTZ 'epoch'+($5::JSONB->>'created')::BIGINT*INTERVAL '1 second', \
                     clock_timestamp()+$9::BIGINT*INTERVAL '1 second')",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .bind(model)
        .bind(body)
        .bind(streamed)
        .bind(&self.pod)
        .bind(&self.instance)
        .bind(self.retention_seconds)
        .execute(&mut *transaction)
        .await?;
        self.insert_record_items(&mut transaction, id, caller, 1, messages)
            .await?;
        transaction.commit().await?;
        Ok(())
    }

    /// Lists one retained archive page with cursor validation in the same caller and filter scope.
    pub(crate) async fn chat_list(
        &self,
        caller: &Caller,
        query: &PageQuery,
        model: Option<&str>,
        metadata: Option<&Value>,
    ) -> Result<Value, StoreError> {
        let (limit, order) = query.bounds(Order::Asc)?;
        let direction = match order {
            Order::Asc => "ASC",
            Order::Desc => "DESC",
        };
        let comparison = if matches!(order, Order::Asc) == query.after.is_some() {
            ">"
        } else {
            "<"
        };
        let cursor = query.after.as_deref().or(query.before.as_deref());
        let mut transaction = self.pool().await?.begin().await?;
        if let Some(cursor) = cursor {
            // Pin the cursor so expiry collection cannot remove its timestamp between queries.
            sqlx::query(
                "SELECT id FROM foretoken_api_records \
                 WHERE namespace=$1 AND caller IS NOT DISTINCT FROM $2 AND kind='chat' \
                 AND stored AND NOT deleted AND expires_at>clock_timestamp() \
                 AND ($3::TEXT IS NULL OR model=$3) \
                 AND ($4::JSONB IS NULL OR body->'metadata' @> $4) AND id=$5 FOR SHARE",
            )
            .bind(&self.namespace)
            .bind(caller)
            .bind(model)
            .bind(metadata)
            .bind(cursor)
            .fetch_optional(&mut *transaction)
            .await?
            .ok_or_else(|| StoreError::Invalid("the pagination cursor was not found".into()))?;
        }
        let sql = format!(
            "SELECT r.body FROM foretoken_api_records r \
             WHERE r.namespace=$1 AND r.caller IS NOT DISTINCT FROM $2 AND r.kind='chat' \
             AND r.stored AND NOT r.deleted AND r.expires_at>clock_timestamp() \
             AND ($3::TEXT IS NULL OR r.model=$3) \
             AND ($4::JSONB IS NULL OR r.body->'metadata' @> $4) \
             AND ($5::TEXT IS NULL OR (r.created_at,r.id) {comparison} \
                 (SELECT created_at,id FROM foretoken_api_records WHERE namespace=$1 AND id=$5)) \
             ORDER BY r.created_at {direction},r.id {direction} LIMIT $6"
        );
        let bodies = sqlx::query_scalar(&sql)
            .bind(&self.namespace)
            .bind(caller)
            .bind(model)
            .bind(metadata)
            .bind(cursor)
            .bind((limit + 1) as i64)
            .fetch_all(&mut *transaction)
            .await?;
        transaction.commit().await?;
        Ok(list_page(bodies, limit))
    }

    /// Retrieves the exact final Chat Completion retained for this ingress caller.
    pub(crate) async fn chat_get(&self, caller: &Caller, id: &str) -> Result<Value, StoreError> {
        sqlx::query_scalar(
            "SELECT body FROM foretoken_api_records \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND kind='chat' \
             AND stored AND NOT deleted AND expires_at>clock_timestamp()",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .fetch_optional(self.pool().await?)
        .await?
        .ok_or(StoreError::NotFound)
    }

    /// Lists a bounded page of original inputs while retaining archive membership through the read.
    pub(crate) async fn chat_messages(
        &self,
        caller: &Caller,
        id: &str,
        query: &PageQuery,
    ) -> Result<Value, StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        sqlx::query(
            "SELECT id FROM foretoken_api_records \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND kind='chat' \
             AND stored AND NOT deleted AND expires_at>clock_timestamp() FOR SHARE",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or(StoreError::NotFound)?;
        let messages = self
            .record_input_page(&mut transaction, id, query, Order::Asc)
            .await?;
        transaction.commit().await?;
        Ok(messages)
    }

    /// Replaces only the public metadata of a retained completion.
    pub(crate) async fn chat_update(
        &self,
        caller: &Caller,
        id: &str,
        metadata: &Value,
    ) -> Result<Value, StoreError> {
        sqlx::query_scalar(
            "UPDATE foretoken_api_records SET body=jsonb_set(body,'{metadata}',$4) \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND kind='chat' \
             AND stored AND NOT deleted AND expires_at>clock_timestamp() RETURNING body",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .bind(metadata)
        .fetch_optional(self.pool().await?)
        .await?
        .ok_or(StoreError::NotFound)
    }

    /// Marks a retained completion deleted without modifying shared immutable items.
    pub(crate) async fn chat_delete(&self, caller: &Caller, id: &str) -> Result<Value, StoreError> {
        let deleted = sqlx::query(
            "UPDATE foretoken_api_records SET deleted=TRUE,expires_at=clock_timestamp() \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND kind='chat' \
             AND stored AND NOT deleted AND expires_at>clock_timestamp()",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(caller)
        .execute(self.pool().await?)
        .await?;
        if deleted.rows_affected() == 0 {
            return Err(StoreError::NotFound);
        }
        Ok(json!({"id":id,"object":"chat.completion.deleted","deleted":true}))
    }
}
