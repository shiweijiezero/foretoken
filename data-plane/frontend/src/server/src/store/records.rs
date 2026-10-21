// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Cached Responses, immutable history views, cancellation intent, and replay events.

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use super::{
    ApiStore, Caller, Order, PageQuery, StoreError, cache_items, decode_items, decode_json,
    list_page, page_input,
};

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
    pub remaining: std::time::Duration,
}

pub(crate) struct StoredResponse {
    pub body: Value,
    pub status: String,
    pub background: bool,
    pub streamed: bool,
    pub replay_expired: bool,
}

/// Locates a cached response for maintenance even after its service's last frontend stops.
#[derive(Deserialize, Serialize)]
pub(crate) struct ResponseRef {
    pub scope_prefix: String,
    pub id: String,
}

/// Recovery facts; the ledger, rather than a local routing snapshot, decides retirement.
pub(crate) struct PendingResponse {
    pub response: ResponseRef,
    pub reservation: Option<Value>,
    pub execution_pod: String,
    pub execution_instance: String,
    pub local_execution: bool,
    pub process_replaced: bool,
    pub service_replaced: bool,
    pub deadline_expired: bool,
}

impl ApiStore {
    /// Resolves a saved predecessor for a stateless continuation without writing the new turn.
    pub(crate) async fn history(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<Vec<Value>, StoreError> {
        decode_items(self.transition("history", caller, json!({"id":id})).await?)
    }

    /// Claims a conversation and pins the complete input view before execution can start.
    pub(crate) async fn begin_response(
        &self,
        request: NewResponse,
    ) -> Result<Vec<Value>, StoreError> {
        let model = request
            .body
            .get("model")
            .and_then(Value::as_str)
            .ok_or(StoreError::Corrupt)?;
        let input = json!({
            "id":request.id, "model":model, "body":request.body.to_string(),
            "stored":u8::from(request.stored), "background":u8::from(request.background),
            "streamed":u8::from(request.streamed), "previous":request.previous,
            "conversation":request.conversation,
            "pod":self.pod, "instance":self.instance,
            "ledger":self.ledger.as_deref().ok_or(StoreError::Configuration)?,
            "reservation":request.reservation.map(|value| value.to_string()).unwrap_or_default(),
            "items":cache_items(&request.input)?,
            "retention":self.retention_seconds, "replay":self.replay_seconds,
            "remaining_millis":u64::try_from(request.remaining.as_millis()).map_err(|_| StoreError::Corrupt)?,
        });
        decode_items(self.transition("begin", &request.caller, input).await?)
    }

    /// Discards a request rejected during lowering, including its conversation claim.
    pub(crate) async fn discard_response(&self, id: &str) -> Result<(), StoreError> {
        self.transition("discard", &None, json!({"id":id,"instance":self.instance}))
            .await
    }

    /// Returns a public snapshot without consulting a model runtime.
    pub(crate) async fn response(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<StoredResponse, StoreError> {
        let (body, status, background, streamed, replay_expired): (
            String,
            String,
            bool,
            bool,
            bool,
        ) = self
            .transition("response", caller, json!({"id":id}))
            .await?;
        Ok(StoredResponse {
            body: decode_json(body)?,
            status,
            background,
            streamed,
            replay_expired,
        })
    }

    /// Reads one page of the exact expanded input view retained by a response.
    pub(crate) async fn response_inputs(
        &self,
        caller: &Caller,
        id: &str,
        query: &PageQuery,
    ) -> Result<Value, StoreError> {
        let (limit, input) = page_input(id, query, Order::Desc)?;
        Ok(list_page(
            decode_items(self.transition("response_inputs", caller, input).await?)?,
            limit,
        ))
    }

    /// Records cancellation intent; the execution owner confirms termination separately.
    pub(crate) async fn cancel_response(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<Value, StoreError> {
        decode_json(self.transition("cancel", caller, json!({"id":id})).await?)
    }

    /// Hides a response without cancelling its execution or deleting another history view.
    pub(crate) async fn delete_response(
        &self,
        caller: &Caller,
        id: &str,
    ) -> Result<(), StoreError> {
        self.transition("delete_response", caller, json!({"id":id}))
            .await
    }

    /// Observes remote cancellation without letting notifications replace the execution-owner check.
    pub(crate) async fn wait_cancelled(&self, id: &str) -> Result<(), StoreError> {
        let mut changes = self.cancellation_changes(id);
        while !self.cancellation_requested(id).await? {
            changes.changed().await;
        }
        Ok(())
    }

    /// Reads cancellation independently of HTTP consumers and detects a lost cache record.
    async fn cancellation_requested(&self, id: &str) -> Result<bool, StoreError> {
        self.transition(
            "cancellation_requested",
            &None,
            json!({"id":id,"instance":self.instance}),
        )
        .await
    }

    /// Commits response progress and an ordered batch of replay events before publication.
    pub(crate) async fn publish_response(
        &self,
        id: &str,
        snapshot: Option<&Value>,
        events: &[Value],
    ) -> Result<(), StoreError> {
        let status = snapshot
            .map(|body| {
                body.get("status")
                    .and_then(Value::as_str)
                    .ok_or(StoreError::Corrupt)
            })
            .transpose()?;
        self.transition(
            "publish",
            &None,
            json!({
                "id":id, "instance":self.instance,
                "body":snapshot.map(Value::to_string).unwrap_or_default(),
                "status":status.unwrap_or(""),
                "events":cache_events(events)?
            }),
        )
        .await
    }

    /// Commits a terminal snapshot, history, conversation append and final events atomically.
    pub(crate) async fn finish_response(
        &self,
        id: &str,
        body: &Value,
        events: &[Value],
    ) -> Result<Value, StoreError> {
        self.finish_snapshot(&self.response_ref(id), body, events, &self.instance, "")
            .await
    }

    /// Serializes normal completion and version-checked stopped-owner recovery on the same record.
    async fn finish_snapshot(
        &self,
        response: &ResponseRef,
        body: &Value,
        events: &[Value],
        instance: &str,
        version: &str,
    ) -> Result<Value, StoreError> {
        let status = body
            .get("status")
            .and_then(Value::as_str)
            .ok_or(StoreError::Corrupt)?;
        let items = body
            .get("output")
            .and_then(Value::as_array)
            .ok_or(StoreError::Corrupt)?;
        decode_json(
            self.transition(
                "finish",
                &None,
                json!({
                    "id":response.id, "scope_prefix":response.scope_prefix,
                    "instance":instance, "version":version,
                    "body":body.to_string(), "status":status, "items":cache_items(items)?,
                    "events":cache_events(events)?,
                }),
            )
            .await?,
        )
    }

    /// Loads a bounded event page after the caller has checked access and replay lifetime.
    pub(crate) async fn events(&self, id: &str, after: i64) -> Result<Vec<Value>, StoreError> {
        let events: Vec<String> = self
            .transition("events", &None, json!({"id":id,"after":after}))
            .await?;
        events.into_iter().map(decode_json).collect()
    }

    /// Checks that a resume cursor names a committed event, including streams with interrupted batches.
    pub(crate) async fn has_event(&self, id: &str, sequence: i64) -> Result<bool, StoreError> {
        self.transition("has_event", &None, json!({"id":id,"sequence":sequence}))
            .await
    }

    /// Lists unfinished records with process facts; the ledger owns membership and termination.
    pub(crate) async fn pending_responses(&self) -> Result<Vec<PendingResponse>, StoreError> {
        type PendingRow = (String, String, String, String, String, bool, bool, bool);
        let rows: Vec<PendingRow> = self
            .transition(
                "pending",
                &None,
                json!({"instance":self.instance,"ledger":self.ledger}),
            )
            .await?;
        rows.into_iter()
            .map(
                |(
                    scope_prefix,
                    id,
                    reservation,
                    execution_pod,
                    execution_instance,
                    local_execution,
                    process_replaced,
                    deadline_expired,
                )| {
                    let reservation = if reservation.is_empty() {
                        None
                    } else {
                        Some(decode_json(reservation)?)
                    };
                    let service_replaced = scope_prefix == self.prefix
                        && reservation
                            .as_ref()
                            .and_then(|value| value.get("scope"))
                            .and_then(Value::as_str)
                            != Some(self.execution_scope.as_str());
                    Ok(PendingResponse {
                        response: ResponseRef { scope_prefix, id },
                        reservation,
                        execution_pod,
                        execution_instance,
                        local_execution,
                        process_replaced,
                        service_replaced,
                        deadline_expired,
                    })
                },
            )
            .collect()
    }

    /// Re-reads the preparation boundary before recovery acts on a retired or overdue execution.
    pub(crate) async fn pending_started(
        &self,
        response: &ResponseRef,
        instance: &str,
    ) -> Result<Option<bool>, StoreError> {
        let status: String = self
            .transition(
                "pending_started",
                &None,
                json!({"id":response.id,"scope_prefix":response.scope_prefix,"instance":instance}),
            )
            .await?;
        match status.as_str() {
            "" => Ok(None),
            "0" => Ok(Some(false)),
            "1" => Ok(Some(true)),
            _ => Err(StoreError::Corrupt),
        }
    }

    /// Commits failure from the last checkpoint without overwriting a concurrent update or terminal winner.
    pub(crate) async fn fail_response(
        &self,
        response: &ResponseRef,
        error: Value,
    ) -> Result<(), StoreError> {
        loop {
            let snapshot: Vec<String> = self
                .transition("failure_snapshot", &None, json!(response))
                .await?;
            if snapshot.is_empty() {
                return Ok(());
            }
            let [body, version, next_sequence] = snapshot.as_slice() else {
                return Err(StoreError::Corrupt);
            };
            let mut body = decode_json(body.clone())?;
            body["status"] = json!("failed");
            body["error"] = error.clone();
            if let Some(items) = body.get_mut("output").and_then(Value::as_array_mut) {
                for item in items {
                    if item.get("status").and_then(Value::as_str) == Some("in_progress") {
                        item["status"] = json!("incomplete");
                    }
                }
            }
            let sequence = next_sequence
                .parse::<u64>()
                .map_err(|_| StoreError::Corrupt)?;
            let event =
                json!({"type":"response.failed","sequence_number":sequence,"response":body});
            match self
                .finish_snapshot(response, &body, &[event], "", version)
                .await
            {
                Err(StoreError::Changed) => continue,
                Ok(_) | Err(StoreError::NotFound) => return Ok(()),
                Err(error) => return Err(error),
            }
        }
    }

    /// Retains execution references until the capacity authority accepts completion cleanup.
    pub(crate) async fn pending_acknowledgements(
        &self,
    ) -> Result<Vec<(ResponseRef, Value)>, StoreError> {
        let rows: Vec<(String, String, String)> = self
            .transition(
                "pending_acknowledgements",
                &None,
                json!({"ledger":self.ledger}),
            )
            .await?;
        rows.into_iter()
            .map(|(scope_prefix, id, reference)| {
                Ok((ResponseRef { scope_prefix, id }, decode_json(reference)?))
            })
            .collect()
    }

    /// Releases termination evidence without changing the public result or its retention.
    pub(crate) async fn acknowledged_response(
        &self,
        response: &ResponseRef,
    ) -> Result<(), StoreError> {
        self.transition("acknowledged", &None, json!(response))
            .await
    }

    /// Reclaims expired objects, replay logs and items no longer referenced by surviving views.
    pub(crate) async fn collect_expired(&self) -> Result<(), StoreError> {
        self.transition("collect", &None, json!({})).await
    }
}

/// Separates event cursor keys from opaque JSON for atomic, sequence-ordered replay writes.
fn cache_events(events: &[Value]) -> Result<Vec<(u64, String)>, StoreError> {
    events
        .iter()
        .map(|event| {
            let sequence = event
                .get("sequence_number")
                .and_then(Value::as_u64)
                .ok_or(StoreError::Corrupt)?;
            Ok((sequence, event.to_string()))
        })
        .collect()
}
