// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Saved Responses, immutable history views, cancellation intent, and durable SSE events.

use serde_json::{Value, json};
use sqlx::{FromRow, Row};

use super::{ApiStore, Caller, Order, PageQuery, StoreError};

pub(crate) struct NewResponse {
    pub id: String,
    pub caller: Caller,
    pub body: Value,
    pub stored: bool,
    pub background: bool,
    pub streamed: bool,
    pub previous: Option<String>,
    pub conversation: Option<String>,
    pub input: Vec<Value>,
    pub reservation: Option<Value>,
}

#[derive(FromRow)]
pub(crate) struct StoredResponse {
    pub body: Value,
    pub status: String,
    pub background: bool,
    pub streamed: bool,
    pub replay_expired: bool,
}

/// Recovery facts; the ledger, rather than a local routing snapshot, decides retirement.
#[derive(FromRow)]
pub(crate) struct PendingResponse {
    pub id: String,
    pub reservation: Option<Value>,
    pub execution_pod: String,
    pub execution_instance: String,
    pub local_execution: bool,
    pub process_replaced: bool,
    pub same_service: bool,
}

impl ApiStore {
    /// Resolves a saved predecessor for a stateless continuation without writing the new turn.
    pub(crate) async fn history(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<Vec<Value>, StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let status: Option<String> = sqlx::query_scalar(
            "SELECT status FROM foretoken_api_records WHERE namespace=$1 AND id=$2 \
             AND caller IS NOT DISTINCT FROM $3 AND kind='response' AND NOT deleted \
             AND (stored OR background) AND (expires_at IS NULL OR expires_at>clock_timestamp()) FOR SHARE",
        )
        .bind(&self.namespace).bind(id).bind(caller)
        .fetch_optional(&mut *transaction).await?;
        let status = status.ok_or(StoreError::NotFound)?;
        if matches!(status.as_str(), "queued" | "in_progress") {
            return Err(StoreError::ResponseBusy);
        }
        let history = self.record_items(&mut transaction, id).await?;
        transaction.commit().await?;
        Ok(history)
    }

    /// Claims a conversation and pins the complete input view before execution can start.
    pub(crate) async fn begin_response(
        &self,
        request: NewResponse,
    ) -> Result<Vec<Value>, StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let mut history = Vec::new();
        if let Some(previous) = &request.previous {
            let status: Option<String> = sqlx::query_scalar(
                "SELECT status FROM foretoken_api_records WHERE namespace=$1 AND id=$2 \
                 AND caller IS NOT DISTINCT FROM $3 AND kind='response' AND NOT deleted \
                 AND (stored OR background) AND (expires_at IS NULL OR expires_at>clock_timestamp()) FOR SHARE",
            )
            .bind(&self.namespace).bind(previous).bind(&request.caller)
            .fetch_optional(&mut *transaction).await?;
            let status = status.ok_or(StoreError::NotFound)?;
            if matches!(status.as_str(), "queued" | "in_progress") {
                return Err(StoreError::ResponseBusy);
            }
            history = self.record_items(&mut transaction, previous).await?;
        }
        if let Some(conversation) = &request.conversation {
            let row = sqlx::query(
                "SELECT active_response FROM foretoken_api_conversations WHERE namespace=$1 AND id=$2 \
                 AND caller IS NOT DISTINCT FROM $3 AND NOT deleted FOR UPDATE",
            )
            .bind(&self.namespace).bind(conversation).bind(&request.caller)
            .fetch_optional(&mut *transaction).await?.ok_or(StoreError::NotFound)?;
            if row
                .try_get::<Option<String>, _>("active_response")?
                .is_some()
            {
                return Err(StoreError::ConversationBusy);
            }
            history = sqlx::query_scalar(
                "SELECT i.body FROM foretoken_api_conversation_items c \
                 JOIN foretoken_api_items i ON i.namespace=c.namespace AND i.id=c.item_id \
                 WHERE c.namespace=$1 AND c.conversation_id=$2 ORDER BY c.position",
            )
            .bind(&self.namespace)
            .bind(conversation)
            .fetch_all(&mut *transaction)
            .await?;
            sqlx::query("UPDATE foretoken_api_conversations SET active_response=$3 WHERE namespace=$1 AND id=$2")
                .bind(&self.namespace).bind(conversation).bind(&request.id)
                .execute(&mut *transaction).await?;
        }
        let mut input = Vec::with_capacity(request.input.len());
        for item in request.input {
            if item.get("type").and_then(Value::as_str) == Some("item_reference") {
                let id = item
                    .get("id")
                    .and_then(Value::as_str)
                    .ok_or_else(|| StoreError::Invalid("item_reference requires id".into()))?;
                let item = sqlx::query_scalar::<_, Value>(
                    "SELECT body FROM foretoken_api_items WHERE namespace=$1 AND id=$2 \
                     AND caller IS NOT DISTINCT FROM $3 FOR KEY SHARE",
                )
                .bind(&self.namespace)
                .bind(id)
                .bind(&request.caller)
                .fetch_optional(&mut *transaction)
                .await?
                .ok_or(StoreError::NotFound)?;
                input.push(item);
            } else {
                input.push(item);
            }
        }
        // A repeated reference is another input occurrence, not an ambiguous pagination cursor.
        let mut seen: std::collections::HashSet<String> = history
            .iter()
            .filter_map(|item| item.get("id").and_then(Value::as_str).map(str::to_owned))
            .collect();
        for item in &mut input {
            let id = item
                .get("id")
                .and_then(Value::as_str)
                .ok_or(StoreError::Corrupt)?;
            if !seen.insert(id.to_owned()) {
                item["id"] = json!(format!("item_{}", uuid::Uuid::new_v4().simple()));
            }
        }
        let model = request
            .body
            .get("model")
            .and_then(Value::as_str)
            .ok_or(StoreError::Corrupt)?;
        sqlx::query(
            "INSERT INTO foretoken_api_records \
             (namespace,id,caller,kind,model,body,status,stored,background,streamed,conversation_id,execution_pod,execution_instance,reservation) \
             VALUES ($1,$2,$3,'response',$4,$5,'queued',$6,$7,$8,$9,$10,$11,$12)",
        )
        .bind(&self.namespace).bind(&request.id).bind(&request.caller).bind(model).bind(&request.body)
        .bind(request.stored).bind(request.background).bind(request.streamed).bind(&request.conversation)
        .bind(&self.pod).bind(&self.instance).bind(&request.reservation)
        .execute(&mut *transaction).await?;
        self.insert_record_items(&mut transaction, &request.id, &request.caller, 0, &history)
            .await?;
        self.insert_record_items(&mut transaction, &request.id, &request.caller, 1, &input)
            .await?;
        transaction.commit().await?;
        history.extend(input);
        Ok(history)
    }

    /// Discards a request rejected during lowering, including its conversation claim.
    pub(crate) async fn discard_response(&self, id: &str) -> Result<(), StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        sqlx::query("UPDATE foretoken_api_conversations SET active_response=NULL WHERE namespace=$1 AND active_response=$2")
            .bind(&self.namespace).bind(id).execute(&mut *transaction).await?;
        sqlx::query("DELETE FROM foretoken_api_records WHERE namespace=$1 AND id=$2 AND execution_instance=$3 AND status='queued'")
            .bind(&self.namespace).bind(id).bind(&self.instance).execute(&mut *transaction).await?;
        transaction.commit().await?;
        Ok(())
    }

    /// Returns a public snapshot without consulting a model runtime.
    pub(crate) async fn response(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<StoredResponse, StoreError> {
        sqlx::query_as(
            "SELECT body,status,background,streamed, \
             (replay_until IS NOT NULL AND replay_until<=clock_timestamp()) AS replay_expired \
             FROM foretoken_api_records WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 \
             AND kind='response' AND NOT deleted AND (stored OR background) \
             AND (expires_at IS NULL OR expires_at>clock_timestamp())",
        )
        .bind(&self.namespace).bind(id).bind(caller)
        .fetch_optional(self.pool().await?).await?.ok_or(StoreError::NotFound)
    }

    /// Reads one page of the exact expanded input view retained by a response.
    pub(crate) async fn response_inputs(
        &self,
        caller: &Caller,
        id: &str,
        query: &PageQuery,
    ) -> Result<Value, StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let found: Option<String> = sqlx::query_scalar(
            "SELECT id FROM foretoken_api_records WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 \
             AND kind='response' AND NOT deleted AND (stored OR background) \
             AND (expires_at IS NULL OR expires_at>clock_timestamp()) FOR SHARE",
        )
        .bind(&self.namespace).bind(id).bind(caller).fetch_optional(&mut *transaction).await?;
        found.ok_or(StoreError::NotFound)?;
        let items = self
            .record_input_page(&mut transaction, id, query, Order::Desc)
            .await?;
        transaction.commit().await?;
        Ok(items)
    }

    /// Persists cancellation intent; the execution owner confirms termination separately.
    pub(crate) async fn cancel_response(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<Value, StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let row = sqlx::query(
            "SELECT body,status,background FROM foretoken_api_records WHERE namespace=$1 AND id=$2 \
             AND caller IS NOT DISTINCT FROM $3 AND kind='response' AND NOT deleted \
             AND (stored OR background) AND (expires_at IS NULL OR expires_at>clock_timestamp()) FOR UPDATE",
        )
        .bind(&self.namespace).bind(id).bind(caller)
        .fetch_optional(&mut *transaction).await?.ok_or(StoreError::NotFound)?;
        if !row.try_get::<bool, _>("background")? {
            return Err(StoreError::Invalid(
                "only background responses can be cancelled".into(),
            ));
        }
        let status: String = row.try_get("status")?;
        if matches!(status.as_str(), "queued" | "in_progress") {
            sqlx::query("UPDATE foretoken_api_records SET cancel_requested=TRUE WHERE namespace=$1 AND id=$2")
                .bind(&self.namespace).bind(id).execute(&mut *transaction).await?;
        }
        let body = row.try_get("body")?;
        transaction.commit().await?;
        Ok(body)
    }

    /// Hides a response without cancelling its independent execution or deleting other history views.
    pub(crate) async fn delete_response(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<(), StoreError> {
        let count = sqlx::query(
            "UPDATE foretoken_api_records SET deleted=TRUE, \
             expires_at=CASE WHEN status IN ('queued','in_progress') THEN expires_at ELSE clock_timestamp() END \
             WHERE namespace=$1 AND id=$2 AND caller IS NOT DISTINCT FROM $3 AND kind='response' \
             AND NOT deleted AND (stored OR background) AND (expires_at IS NULL OR expires_at>clock_timestamp())",
        )
        .bind(&self.namespace).bind(id).bind(caller)
        .execute(self.pool().await?).await?.rows_affected();
        if count == 0 {
            return Err(StoreError::NotFound);
        }
        Ok(())
    }

    /// Reads cancellation independently of HTTP consumers and snapshot replacement.
    pub(crate) async fn cancellation_requested(&self, id: &str) -> Result<bool, StoreError> {
        let row = sqlx::query(
            "SELECT cancel_requested,status FROM foretoken_api_records \
             WHERE namespace=$1 AND id=$2 AND execution_instance=$3",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(&self.instance)
        .fetch_optional(self.pool().await?)
        .await?
        .ok_or(StoreError::NotFound)?;
        let status: String = row.try_get("status")?;
        Ok(row.try_get::<bool, _>("cancel_requested")?
            || !matches!(status.as_str(), "queued" | "in_progress"))
    }

    /// Commits response progress and an ordered batch of replay events before publication.
    pub(crate) async fn publish_response(
        &self,
        id: &str,
        snapshot: Option<&Value>,
        events: &[Value],
    ) -> Result<(), StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let status: Option<String> = sqlx::query_scalar(
            "SELECT status FROM foretoken_api_records WHERE namespace=$1 AND id=$2 AND execution_instance=$3 FOR UPDATE",
        )
        .bind(&self.namespace).bind(id).bind(&self.instance)
        .fetch_optional(&mut *transaction).await?;
        let status = status.ok_or(StoreError::NotFound)?;
        if !matches!(status.as_str(), "queued" | "in_progress") {
            return Err(StoreError::ResponseBusy);
        }
        if let Some(snapshot) = snapshot {
            let status = snapshot
                .get("status")
                .and_then(Value::as_str)
                .ok_or(StoreError::Corrupt)?;
            sqlx::query(
                "UPDATE foretoken_api_records SET body=$3,status=$4 WHERE namespace=$1 AND id=$2",
            )
            .bind(&self.namespace)
            .bind(id)
            .bind(snapshot)
            .bind(status)
            .execute(&mut *transaction)
            .await?;
        }
        self.insert_events(&mut transaction, id, events).await?;
        transaction.commit().await?;
        Ok(())
    }

    /// Commits a terminal snapshot, history, conversation append and final events atomically.
    pub(crate) async fn finish_response(
        &self,
        id: &str,
        body: &Value,
        events: &[Value],
    ) -> Result<Value, StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let row = sqlx::query(
            "SELECT body,caller,status,conversation_id FROM foretoken_api_records \
             WHERE namespace=$1 AND id=$2 AND execution_instance=$3 FOR UPDATE",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(&self.instance)
        .fetch_optional(&mut *transaction)
        .await?
        .ok_or(StoreError::NotFound)?;
        self.finish_transaction(transaction, id, body, events, row)
            .await
    }

    /// Serializes normal completion and stopped-owner recovery on the response row.
    async fn finish_transaction(
        &self,
        mut transaction: sqlx::Transaction<'_, sqlx::Postgres>,
        id: &str,
        body: &Value,
        events: &[Value],
        row: sqlx::postgres::PgRow,
    ) -> Result<Value, StoreError> {
        let current: String = row.try_get("status")?;
        if !matches!(current.as_str(), "queued" | "in_progress") {
            // Publication follows the committed winner, including recovery or a lost commit reply.
            return Ok(row.try_get("body")?);
        }
        let caller: Caller = row.try_get("caller")?;
        let conversation: Option<String> = row.try_get("conversation_id")?;
        let status = body
            .get("status")
            .and_then(Value::as_str)
            .ok_or(StoreError::Corrupt)?;
        let output = body
            .get("output")
            .and_then(Value::as_array)
            .ok_or(StoreError::Corrupt)?;
        self.insert_record_items(&mut transaction, id, &caller, 2, output)
            .await?;
        if let Some(conversation) = conversation {
            let row = sqlx::query(
                "SELECT active_response,deleted FROM foretoken_api_conversations WHERE namespace=$1 AND id=$2 FOR UPDATE",
            )
            .bind(&self.namespace).bind(&conversation).fetch_one(&mut *transaction).await?;
            let active: Option<String> = row.try_get("active_response")?;
            if active.as_deref() == Some(id) {
                if !row.try_get::<bool, _>("deleted")?
                    && (matches!(status, "completed" | "incomplete") || !output.is_empty())
                {
                    let mut new_items: Vec<Value> = sqlx::query_scalar(
                        "SELECT i.body FROM foretoken_api_record_items r \
                         JOIN foretoken_api_items i ON i.namespace=r.namespace AND i.id=r.item_id \
                         WHERE r.namespace=$1 AND r.record_id=$2 AND r.stage=1 ORDER BY r.position",
                    )
                    .bind(&self.namespace)
                    .bind(id)
                    .fetch_all(&mut *transaction)
                    .await?;
                    new_items.extend(output.iter().cloned());
                    self.insert_conversation_items(
                        &mut transaction,
                        &conversation,
                        &caller,
                        &mut new_items,
                    )
                    .await?;
                }
                sqlx::query("UPDATE foretoken_api_conversations SET active_response=NULL WHERE namespace=$1 AND id=$2")
                    .bind(&self.namespace).bind(&conversation).execute(&mut *transaction).await?;
            }
        }
        sqlx::query(
            "UPDATE foretoken_api_records SET body=$3,status=$4, \
             expires_at=clock_timestamp()+(CASE WHEN deleted OR (NOT stored AND NOT background) THEN 0 \
                 WHEN stored THEN $5::bigint ELSE $6::bigint END)*INTERVAL '1 second', \
             replay_until=clock_timestamp()+$6::bigint*INTERVAL '1 second' WHERE namespace=$1 AND id=$2",
        )
        .bind(&self.namespace).bind(id).bind(body).bind(status)
        .bind(self.retention_seconds).bind(self.replay_seconds)
        .execute(&mut *transaction).await?;
        self.insert_events(&mut transaction, id, events).await?;
        transaction.commit().await?;
        Ok(body.clone())
    }

    async fn insert_events(
        &self,
        transaction: &mut sqlx::Transaction<'_, sqlx::Postgres>,
        id: &str,
        events: &[Value],
    ) -> Result<(), StoreError> {
        for event in events {
            let sequence = event
                .get("sequence_number")
                .and_then(Value::as_i64)
                .ok_or(StoreError::Corrupt)?;
            sqlx::query("INSERT INTO foretoken_api_events (namespace,response_id,sequence,body) VALUES ($1,$2,$3,$4)")
                .bind(&self.namespace).bind(id).bind(sequence).bind(event)
                .execute(&mut **transaction).await?;
        }
        Ok(())
    }

    /// Loads a bounded event page; the caller has already checked object access and replay lifetime.
    pub(crate) async fn events(&self, id: &str, after: i64) -> Result<Vec<Value>, StoreError> {
        Ok(sqlx::query_scalar(
            "SELECT body FROM foretoken_api_events WHERE namespace=$1 AND response_id=$2 AND sequence>$3 \
             ORDER BY sequence LIMIT 256",
        )
        .bind(&self.namespace).bind(id).bind(after).fetch_all(self.pool().await?).await?)
    }

    /// Checks a resume cursor without silently skipping nonexistent history.
    pub(crate) async fn last_sequence(&self, id: &str) -> Result<Option<i64>, StoreError> {
        Ok(sqlx::query_scalar(
            "SELECT MAX(sequence) FROM foretoken_api_events WHERE namespace=$1 AND response_id=$2",
        )
        .bind(&self.namespace)
        .bind(id)
        .fetch_one(self.pool().await?)
        .await?)
    }

    /// Lists unfinished records with process facts, without inferring death from a stale local view.
    pub(crate) async fn pending_responses(&self) -> Result<Vec<PendingResponse>, StoreError> {
        Ok(sqlx::query_as(
            "SELECT r.id,r.reservation,r.execution_pod,r.execution_instance, r.execution_instance=$2 AS local_execution, \
             COALESCE(f.instance<>r.execution_instance,FALSE) AS process_replaced, \
             COALESCE(r.reservation->>'scope'=$3,FALSE) AS same_service \
             FROM foretoken_api_records r LEFT JOIN foretoken_api_frontends f \
             ON f.namespace=r.namespace AND f.pod=r.execution_pod \
             WHERE r.namespace=$1 AND r.kind='response' AND r.status IN ('queued','in_progress')",
        ).bind(&self.namespace).bind(&self.instance).bind(&self.execution_scope)
            .fetch_all(self.pool().await?).await?)
    }

    /// Re-reads the preparation boundary after the owning task is known to have stopped.
    pub(crate) async fn pending_started(
        &self,
        id: &str,
        instance: &str,
    ) -> Result<Option<bool>, StoreError> {
        Ok(sqlx::query_scalar(
            "SELECT status='in_progress' FROM foretoken_api_records WHERE namespace=$1 AND id=$2 \
             AND execution_instance=$3 AND status IN ('queued','in_progress')",
        )
        .bind(&self.namespace)
        .bind(id)
        .bind(instance)
        .fetch_optional(self.pool().await?)
        .await?)
    }

    /// Adopts only the failed outcome of a stopped execution; generation is never restarted.
    pub(crate) async fn fail_abandoned(&self, id: &str) -> Result<(), StoreError> {
        self.fail_response(
            id,
            json!({"code":"execution_lost","message":"The response execution process stopped"}),
        )
        .await
    }

    /// Records a failed execution from its last committed output checkpoint.
    pub(crate) async fn fail_response(&self, id: &str, error: Value) -> Result<(), StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        let row = sqlx::query(
            "SELECT body,status,caller,conversation_id FROM foretoken_api_records WHERE namespace=$1 AND id=$2 FOR UPDATE",
        )
        .bind(&self.namespace)
        .bind(id)
        .fetch_optional(&mut *transaction)
        .await?;
        let Some(row) = row else {
            return Ok(());
        };
        let status: String = row.try_get("status")?;
        if !matches!(status.as_str(), "queued" | "in_progress") {
            return Ok(());
        }
        let mut body: Value = row.try_get("body")?;
        body["status"] = json!("failed");
        body["error"] = error;
        if let Some(items) = body.get_mut("output").and_then(Value::as_array_mut) {
            for item in items {
                if item.get("status").and_then(Value::as_str) == Some("in_progress") {
                    item["status"] = json!("incomplete");
                }
            }
        }
        let last: Option<i64> = sqlx::query_scalar(
            "SELECT MAX(sequence) FROM foretoken_api_events WHERE namespace=$1 AND response_id=$2",
        )
        .bind(&self.namespace)
        .bind(id)
        .fetch_one(&mut *transaction)
        .await?;
        let event = json!({"type":"response.failed","sequence_number":last.map_or(0, |value| value+1),"response":body});
        self.finish_transaction(transaction, id, &body, &[event], row)
            .await
            .map(|_| ())
    }

    /// Retains the execution reference until the capacity authority has accepted completion cleanup.
    pub(crate) async fn pending_acknowledgements(
        &self,
    ) -> Result<Vec<(String, Value)>, StoreError> {
        let rows = sqlx::query(
            "SELECT id,reservation FROM foretoken_api_records WHERE namespace=$1 \
             AND status NOT IN ('queued','in_progress') AND reservation IS NOT NULL",
        )
        .bind(&self.namespace)
        .fetch_all(self.pool().await?)
        .await?;
        rows.into_iter()
            .map(|row| Ok((row.try_get("id")?, row.try_get("reservation")?)))
            .collect()
    }

    /// Releases only the saved termination reference; the public result and its retention are unchanged.
    pub(crate) async fn acknowledged_response(&self, id: &str) -> Result<(), StoreError> {
        sqlx::query("UPDATE foretoken_api_records SET reservation=NULL WHERE namespace=$1 AND id=$2 AND status NOT IN ('queued','in_progress')")
            .bind(&self.namespace).bind(id).execute(self.pool().await?).await?;
        Ok(())
    }

    /// Removes expired views and replay logs, then reclaims items with no surviving references.
    pub(crate) async fn collect_expired(&self) -> Result<(), StoreError> {
        let mut transaction = self.pool().await?.begin().await?;
        sqlx::query(
            "DELETE FROM foretoken_api_events e USING foretoken_api_records r \
             WHERE r.namespace=$1 AND e.namespace=r.namespace AND e.response_id=r.id AND r.replay_until<=clock_timestamp()",
        )
        .bind(&self.namespace).execute(&mut *transaction).await?;
        sqlx::query("DELETE FROM foretoken_api_records WHERE namespace=$1 AND expires_at<=clock_timestamp() AND reservation IS NULL")
            .bind(&self.namespace).execute(&mut *transaction).await?;
        // Foreign keys arbitrate concurrent new references; locked items are reconsidered later.
        let ids: Vec<String> = sqlx::query_scalar(
            "SELECT i.id FROM foretoken_api_items i WHERE i.namespace=$1 \
             AND NOT EXISTS (SELECT 1 FROM foretoken_api_record_items r WHERE r.namespace=i.namespace AND r.item_id=i.id) \
             AND NOT EXISTS (SELECT 1 FROM foretoken_api_conversation_items c WHERE c.namespace=i.namespace AND c.item_id=i.id) \
             FOR UPDATE OF i SKIP LOCKED",
        )
        .bind(&self.namespace).fetch_all(&mut *transaction).await?;
        if !ids.is_empty() {
            sqlx::query("DELETE FROM foretoken_api_items WHERE namespace=$1 AND id=ANY($2)")
                .bind(&self.namespace)
                .bind(ids)
                .execute(&mut *transaction)
                .await?;
        }
        transaction.commit().await?;
        Ok(())
    }
}
