// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Executes one response while retaining admission, persistence, and transport cancellation ownership.

use std::sync::Arc;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use foretoken_admission::{
    AdmissionPermit, AdmissionRequest, CallerIdentity, ExecutionReservation, with_caller,
};
use foretoken_chat::{ChatEvent, ChatRequest, FinishReason};
use futures::StreamExt;
use serde_json::{Value, json};
use tokio::sync::{mpsc, oneshot};
use tokio_util::sync::CancellationToken;

use super::super::convert::{self, ResponseMeta, build_response, build_usage};
use super::super::error::ApiError;
use super::super::streaming::{
    OutputItemStreamer, ResponseStreamEvent, response_lifecycle_event, restore_tool_event,
};
use super::super::types::{ResponseItemStatus, ResponsesInput, ResponsesRequest};
use super::{Disconnect, ResponseDelivery, ResponseReady, ResponseTasks};
use crate::api::RequestTiming;
use crate::api::stream::chat_events;
use crate::runtime::{GenerationError, RequestSnapshot};
use crate::store::{ApiStore, NewResponse, StoreError, input_items};

struct ActiveResponse {
    tasks: Arc<ResponseTasks>,
    id: String,
}

struct ResponseExecution {
    items: OutputItemStreamer,
    id: String,
    snapshot: RequestSnapshot,
    request: ResponsesRequest,
    timing: RequestTiming,
    identity: CallerIdentity,
    cancel: CancellationToken,
    store: Option<Arc<ApiStore>>,
    reservation: Option<ExecutionReservation>,
    recorded: bool,
    registration_attempted: bool,
    generation_started: bool,
    ready: Option<oneshot::Sender<Result<ResponseReady, ApiError>>>,
    delivery: mpsc::Sender<Vec<Value>>,
    sequence: u64,
    created_at: u64,
}

impl ResponseTasks {
    /// Starts exactly one execution, with HTTP delivery chosen independently of response storage.
    pub(crate) fn start(
        self: &Arc<Self>,
        snapshot: RequestSnapshot,
        request: ResponsesRequest,
        timing: RequestTiming,
    ) -> ResponseDelivery {
        let id = format!("resp_{}", uuid::Uuid::new_v4().simple());
        let identity = CallerIdentity::current();
        let background = request.background.unwrap_or(false);
        let cancel = self.stopping.child_token();
        let disconnect = Disconnect((!background).then(|| cancel.clone()));
        let (ready, receiver) = oneshot::channel();
        let (events, event_receiver) = mpsc::channel(1);
        self.active
            .lock()
            .expect("response task lock poisoned")
            .insert(id.clone(), cancel.clone());
        let tasks = self.clone();
        self.tracker.spawn(async move {
            let _active = ActiveResponse {
                tasks: tasks.clone(),
                id: id.clone(),
            };
            let items = OutputItemStreamer::new(request.include_reasoning);
            let mut run = ResponseExecution {
                items,
                id,
                snapshot,
                request,
                timing,
                identity: identity.clone(),
                cancel,
                store: tasks.store.clone(),
                reservation: None,
                recorded: false,
                registration_attempted: false,
                generation_started: false,
                ready: Some(ready),
                delivery: events,
                sequence: 0,
                created_at: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs(),
            };
            with_caller(identity, async {
                if let Err(error) = run.execute().await {
                    run.fail(error).await;
                }
            })
            .await;
        });
        ResponseDelivery {
            ready: receiver,
            events: event_receiver,
            disconnect,
        }
    }
}

impl ResponseExecution {
    /// Applies the request budget to registration and generation, including stalled cache operations.
    async fn execute(&mut self) -> Result<(), ApiError> {
        let cancel = self.cancel.clone();
        let deadline =
            tokio::time::Instant::from_std(self.timing.started_at + self.snapshot.request_timeout);
        let (admission, chat, metadata) = tokio::select! {
            biased;
            () = cancel.cancelled() => return Err(ApiError::generation(GenerationError::RequestFailed)),
            () = tokio::time::sleep_until(deadline) => return Err(ApiError::generation(GenerationError::DeadlineExceeded)),
            result = self.prepare() => result?,
        };
        // Leaving this select drops preparation or streaming before termination evidence is awaited.
        let cancelled = tokio::select! {
            biased;
            () = cancel.cancelled() => true,
            () = tokio::time::sleep_until(deadline) => false,
            result = self.generate(&metadata, chat, admission) => return result,
        };
        if cancelled {
            self.finish_cancelled(&metadata, self.created_at).await
        } else {
            self.finish_failure(
                &metadata,
                self.created_at,
                ResponseItemStatus::Failed,
                ApiError::generation(GenerationError::DeadlineExceeded),
            )
            .await
        }
    }

    /// Acquires admission, registers the input view, and transfers the prepared request and permit to generation.
    async fn prepare(&mut self) -> Result<(AdmissionPermit, ChatRequest, ResponseMeta), ApiError> {
        convert::validate_request(&self.request)?;
        let stored = self.request.store.unwrap_or(true);
        let background = self.request.background.unwrap_or(false);
        let streamed = self.request.stream;
        let stateful = stored || background || self.request.conversation.is_some();
        if stateful && self.store.is_none() {
            return Err(StoreError::Disabled.into());
        }
        let model = self.request.model.clone().expect("validated model");
        let admission = self
            .snapshot
            .generation
            .admit(&AdmissionRequest {
                model,
                caller: self.identity.clone(),
                units: 1,
                received_at: self.timing.started_at,
                track_execution: stateful,
            })
            .await
            .map_err(ApiError::generation)?;
        self.reservation = if stateful {
            Some(
                admission
                    .execution()
                    .ok_or_else(|| ApiError::generation(GenerationError::Internal))?,
            )
        } else {
            None
        };
        let created_at = self.created_at;
        let mut request = self.request.clone();
        let input = input_items(
            serde_json::to_value(&request.input).expect("request input serialization"),
        )?;
        let expanded = if stateful {
            self.registration_attempted = true;
            let store = self.store.as_ref().expect("configured store");
            // Only protocol metadata is saved; ingress headers and credentials never enter the payload.
            let initial = json!({"id":self.id,"object":"response","created_at":created_at,
                "status":"queued","model":request.model,"store":stored,"background":background,
                "output":[],"error":null,"usage":null,"metadata":request.metadata,
                "previous_response_id":request.previous_response_id,
                "conversation":request.conversation.as_ref().map(|reference| json!({"id":reference.id()}))});
            let reservation = self.reservation.as_ref().map(|execution| {
                serde_json::to_value(execution.reference())
                    .expect("execution reference serialization")
            });
            let expanded = store
                .begin_response(NewResponse {
                    id: self.id.clone(),
                    caller: self.identity.caller.clone(),
                    body: initial,
                    stored,
                    background,
                    streamed,
                    previous: request.previous_response_id.clone(),
                    conversation: request
                        .conversation
                        .as_ref()
                        .map(|reference| reference.id().to_owned()),
                    input,
                    reservation,
                    remaining: self
                        .snapshot
                        .request_timeout
                        .saturating_sub(self.timing.started_at.elapsed()),
                })
                .await?;
            self.recorded = true;
            expanded
        } else {
            let mut expanded = if let Some(previous) = &request.previous_response_id {
                self.store
                    .as_ref()
                    .ok_or(StoreError::Disabled)?
                    .history(&self.identity.caller, previous)
                    .await?
            } else {
                Vec::new()
            };
            for item in input {
                if item.get("type").and_then(Value::as_str) == Some("item_reference") {
                    let id = item.get("id").and_then(Value::as_str).ok_or_else(|| {
                        ApiError::invalid_request("item_reference requires id", Some("input"))
                    })?;
                    expanded.push(
                        self.store
                            .as_ref()
                            .ok_or(StoreError::Disabled)?
                            .item(&self.identity.caller, id)
                            .await?,
                    );
                } else {
                    expanded.push(item);
                }
            }
            expanded
        };
        request.input = ResponsesInput::Items(expanded);
        let (mut chat, meta) = match convert::prepare_responses_request(request, self.id.clone()) {
            Ok(prepared) => prepared,
            Err(error) => {
                if self.recorded {
                    self.store
                        .as_ref()
                        .expect("recorded response")
                        .discard_response(&self.id)
                        .await?;
                    self.recorded = false;
                }
                return Err(error);
            }
        };
        // Retained evidence names its cache consumer so completed receipts remain reclaimable after cache loss.
        if let Some(reservation) = &self.reservation {
            let consumer = self
                .store
                .as_ref()
                .expect("recorded response")
                .completion_consumer(&self.id);
            reservation
                .retain_completion(&consumer)
                .await
                .map_err(|_| ApiError::generation(GenerationError::Unavailable))?;
        }
        // Every transport observes the same structured item stream and final item identities.
        chat.intermediate = true;
        Ok((admission, chat, meta))
    }

    /// Runs the shared generation pipeline and commits output; its caller owns cancellation and the deadline.
    async fn generate(
        &mut self,
        meta: &ResponseMeta,
        chat: ChatRequest,
        admission: AdmissionPermit,
    ) -> Result<(), ApiError> {
        let created_at = self.created_at;
        let background = meta.background;
        let streamed = self.request.stream;
        self.publish_start(meta).await?;
        self.generation_started = true;
        let snapshot = self.snapshot.clone();
        let model = meta.model.clone();
        let include_reasoning = meta.include_reasoning;
        let timing = self.timing;
        let mut generation = Box::pin(async move {
            snapshot
                .generate_admitted_chat(model, chat, include_reasoning, timing, Some(admission))
                .await
        });
        let mut cancel_poll = tokio::time::interval(Duration::from_millis(100));
        cancel_poll.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let generated = loop {
            tokio::select! {
                result = &mut generation => break result,
                _ = cancel_poll.tick(), if self.recorded => {
                    if self.store.as_ref().expect("recorded response").cancellation_requested(&self.id).await? {
                        self.cancel.cancel();
                    }
                }
            }
        };
        // Drop preparation/submission before waiting for its backend-owned termination evidence.
        drop(generation);
        let generated = match generated {
            Ok(generated) => generated,
            Err(error) => {
                self.finish_failure(
                    meta,
                    created_at,
                    ResponseItemStatus::Failed,
                    ApiError::generation(error),
                )
                .await?;
                return Ok(());
            }
        };
        let (_, events) = chat_events(generated, self.snapshot.stream_idle)
            .map_err(|_| ApiError::generation(GenerationError::Internal))?;
        // Ready chunks amortize persistence without waiting for another model token.
        let mut events = Box::pin(events.ready_chunks(32));
        if !background && streamed {
            self.ready(Ok(ResponseReady::Streaming));
        }
        loop {
            let event = tokio::select! {
                _ = cancel_poll.tick(), if self.recorded => {
                    if self.store.as_ref().expect("recorded response").cancellation_requested(&self.id).await? {
                        self.cancel.cancel();
                    }
                    continue;
                },
                event = events.next() => event,
            };
            let Some(chunk) = event else {
                drop(events);
                self.finish_failure(
                    meta,
                    created_at,
                    ResponseItemStatus::Failed,
                    ApiError::generation(GenerationError::RequestFailed),
                )
                .await?;
                return Ok(());
            };
            let mut outgoing = Vec::new();
            for event in chunk {
                match event {
                    Ok(ChatEvent::Done {
                        message,
                        usage,
                        finish_reason,
                        ..
                    }) => {
                        let status = match finish_reason {
                            FinishReason::Length => ResponseItemStatus::Incomplete,
                            FinishReason::Abort | FinishReason::Error => ResponseItemStatus::Failed,
                            _ => ResponseItemStatus::Completed,
                        };
                        if status == ResponseItemStatus::Failed {
                            drop(events);
                            self.emit(meta, outgoing, None).await?;
                            self.finish_failure(
                                meta,
                                created_at,
                                status,
                                ApiError::generation(GenerationError::RequestFailed),
                            )
                            .await?;
                            return Ok(());
                        }
                        meta.tool_names
                            .validate_finished_tools(
                                &message,
                                status == ResponseItemStatus::Incomplete,
                            )
                            .map_err(|_| ApiError::generation(GenerationError::BackendProtocol))?;
                        let closing = self.items.finish(status, &meta.tool_names);
                        let response = build_response(
                            meta,
                            &self.id,
                            created_at,
                            self.items.final_output_items(),
                            status,
                            Some(build_usage(&usage)),
                        );
                        let mut final_events = outgoing;
                        final_events.extend(closing);
                        final_events.push(response_lifecycle_event(
                            if status == ResponseItemStatus::Incomplete {
                                "response.incomplete"
                            } else {
                                "response.completed"
                            },
                            &response,
                        ));
                        let mut body =
                            serde_json::to_value(response).expect("response serialization");
                        super::super::restore_tools(&mut body, &meta.tool_names)?;
                        drop(events);
                        self.finish(meta, body, final_events, None).await?;
                        return Ok(());
                    }
                    Ok(event) => outgoing.extend(self.items.on_event(&event, &meta.tool_names)),
                    Err(_) => {
                        drop(events);
                        self.emit(meta, outgoing, None).await?;
                        self.finish_failure(
                            meta,
                            created_at,
                            ResponseItemStatus::Failed,
                            ApiError::generation(GenerationError::RequestFailed),
                        )
                        .await?;
                        return Ok(());
                    }
                }
            }
            self.emit(meta, outgoing, None).await?;
        }
    }

    /// Publishes queued and in-progress state before protected preparation or backend submission.
    async fn publish_start(&mut self, meta: &ResponseMeta) -> Result<(), ApiError> {
        let created_at = self.created_at;
        let background = meta.background;
        let streamed = self.request.stream;
        let initial = response_value(
            meta,
            &self.id,
            created_at,
            &[],
            ResponseItemStatus::Queued,
            None,
        )?;
        if background && streamed {
            let queued = build_response(
                meta,
                &self.id,
                created_at,
                vec![],
                ResponseItemStatus::Queued,
                None,
            );
            self.emit(
                meta,
                vec![response_lifecycle_event("response.created", &queued)],
                Some(initial.clone()),
            )
            .await?;
        } else if self.recorded {
            self.store
                .as_ref()
                .expect("recorded response")
                .publish_response(&self.id, Some(&initial), &[])
                .await?;
        }
        if background {
            self.ready(Ok(ResponseReady::Stored(initial)));
        }
        // A queued record has never entered protected preparation or engine submission.
        let in_progress = build_response(
            meta,
            &self.id,
            created_at,
            vec![],
            ResponseItemStatus::InProgress,
            None,
        );
        let mut started = Vec::new();
        if !background {
            started.push(response_lifecycle_event("response.created", &in_progress));
        }
        started.push(response_lifecycle_event(
            "response.in_progress",
            &in_progress,
        ));
        self.emit(
            meta,
            started,
            Some(serde_json::to_value(&in_progress).expect("response serialization")),
        )
        .await?;
        Ok(())
    }

    /// Checkpoints output before public delivery; background streams also retain each event batch.
    async fn emit(
        &mut self,
        meta: &ResponseMeta,
        events: Vec<ResponseStreamEvent>,
        snapshot: Option<Value>,
    ) -> Result<(), ApiError> {
        let checkpoint = self.recorded
            && (meta.background && self.request.stream
                || events
                    .iter()
                    .any(|event| event.event_type() == "response.output_item.done"));
        let snapshot = if snapshot.is_some() {
            snapshot
        } else if checkpoint {
            Some(response_value(
                meta,
                &self.id,
                self.created_at,
                &self.items.snapshot_output_items(&meta.tool_names),
                ResponseItemStatus::InProgress,
                None,
            )?)
        } else {
            None
        };
        let values = self.encode(meta, events)?;
        if self.recorded && (snapshot.is_some() || (meta.background && self.request.stream)) {
            self.store
                .as_ref()
                .expect("recorded response")
                .publish_response(
                    &self.id,
                    snapshot.as_ref(),
                    if meta.background && self.request.stream {
                        &values
                    } else {
                        &[]
                    },
                )
                .await?;
        }
        if self.request.stream && !meta.background && !values.is_empty() {
            self.deliver(values).await?;
        }
        Ok(())
    }

    /// Assigns per-response event cursors after restoring public tool names.
    fn encode(
        &mut self,
        meta: &ResponseMeta,
        events: Vec<ResponseStreamEvent>,
    ) -> Result<Vec<Value>, ApiError> {
        let mut values = Vec::new();
        for event in events {
            for event in restore_tool_event(event, &meta.tool_names)? {
                values.push(event.to_value(self.sequence));
                self.sequence += 1;
            }
        }
        Ok(values)
    }

    /// Keeps slow HTTP consumers within the same deadline and shutdown boundary as generation.
    async fn deliver(&self, values: Vec<Value>) -> Result<(), ApiError> {
        let deadline =
            tokio::time::Instant::from_std(self.timing.started_at + self.snapshot.request_timeout);
        tokio::select! {
            () = self.cancel.cancelled() => {},
            () = tokio::time::sleep_until(deadline) => return Err(ApiError::generation(GenerationError::DeadlineExceeded)),
            sent = self.delivery.send(values) => if sent.is_err() { self.cancel.cancel(); },
        }
        Ok(())
    }

    /// Publishes the committed terminal outcome only after history and conversation ownership settle.
    async fn finish(
        &mut self,
        meta: &ResponseMeta,
        mut body: Value,
        events: Vec<ResponseStreamEvent>,
        mut error: Option<ApiError>,
    ) -> Result<(), ApiError> {
        let mut values = self.encode(meta, events)?;
        if self.recorded {
            let committed = self
                .store
                .as_ref()
                .expect("recorded response")
                .finish_response(
                    &self.id,
                    &body,
                    if meta.background && self.request.stream {
                        &values
                    } else {
                        &[]
                    },
                )
                .await?;
            if committed != body {
                error = None;
                let status = committed
                    .get("status")
                    .and_then(Value::as_str)
                    .ok_or(StoreError::Corrupt)?;
                values = vec![
                    json!({"type":format!("response.{status}"),"sequence_number":self.sequence,"response":committed}),
                ];
                self.sequence += 1;
            }
            body = committed;
        }
        if let Some(reservation) = &self.reservation
            && reservation.acknowledge().await.is_ok()
            && let Some(store) = &self.store
        {
            let _ = store
                .acknowledged_response(&store.response_ref(&self.id))
                .await;
        }
        if !meta.background {
            if self.ready.is_some()
                && let Some(error) = error
            {
                self.ready(Err(error));
            } else if self.request.stream {
                self.ready(Ok(ResponseReady::Streaming));
                match self.delivery.try_send(values) {
                    Ok(()) => {}
                    Err(mpsc::error::TrySendError::Full(values)) => {
                        let _ = self.deliver(values).await;
                    }
                    Err(mpsc::error::TrySendError::Closed(_)) => self.cancel.cancel(),
                }
            } else {
                self.ready(Ok(ResponseReady::Collected(body)));
            }
        }
        Ok(())
    }

    /// Waits for existing execution guards to finish cancellation, or withdraws an unstarted reservation.
    async fn terminate(&self) -> Result<(), ApiError> {
        if let Some(reservation) = &self.reservation {
            if self.generation_started {
                // Existing preparation and stream guards own cancellation and capacity release.
                reservation
                    .wait_finished()
                    .await
                    .map_err(|_| ApiError::generation(GenerationError::Unavailable))?;
            } else {
                // Registration has not started any non-cancelable preparation or backend work.
                reservation
                    .request_cancel()
                    .await
                    .map_err(|_| ApiError::generation(GenerationError::Unavailable))?;
            }
        }
        Ok(())
    }

    /// Converts partial output to a cancelled response after execution has ended.
    async fn finish_cancelled(
        &mut self,
        meta: &ResponseMeta,
        created_at: u64,
    ) -> Result<(), ApiError> {
        self.terminate().await?;
        let mut events = self
            .items
            .finish(ResponseItemStatus::Incomplete, &meta.tool_names);
        let response = build_response(
            meta,
            &self.id,
            created_at,
            self.items.final_output_items(),
            ResponseItemStatus::Cancelled,
            None,
        );
        let mut body = serde_json::to_value(&response).expect("response serialization");
        super::super::restore_tools(&mut body, &meta.tool_names)?;
        events.push(response_lifecycle_event("response.cancelled", &response));
        self.finish(
            meta,
            body,
            events,
            Some(ApiError::generation(GenerationError::RequestFailed)),
        )
        .await
    }

    /// Commits failure and partial output before returning the transport error.
    async fn finish_failure(
        &mut self,
        meta: &ResponseMeta,
        created_at: u64,
        status: ResponseItemStatus,
        error: ApiError,
    ) -> Result<(), ApiError> {
        self.terminate().await?;
        let mut events = self
            .items
            .finish(ResponseItemStatus::Incomplete, &meta.tool_names);
        let mut response = build_response(
            meta,
            &self.id,
            created_at,
            self.items.final_output_items(),
            status,
            None,
        );
        response.error = Some(error.body.clone());
        let mut body = serde_json::to_value(&response).expect("response serialization");
        super::super::restore_tools(&mut body, &meta.tool_names)?;
        events.push(response_lifecycle_event("response.failed", &response));
        self.finish(meta, body, events, Some(error)).await
    }

    /// Settles execution and conversation ownership before exposing a setup or storage error.
    async fn fail(&mut self, error: ApiError) {
        if self.terminate().await.is_ok() {
            let persisted = if self.registration_attempted {
                let store = self.store.as_ref().expect("recorded response");
                store
                    .fail_response(&store.response_ref(&self.id), error.body.clone())
                    .await
                    .is_ok()
            } else {
                true
            };
            if persisted && let Some(reservation) = &self.reservation {
                let _ = reservation.acknowledge().await;
            }
        }
        if self.ready.is_none() && self.request.stream && !self.request.background.unwrap_or(false)
        {
            let _ = self
                .delivery
                .try_send(vec![json!({"type":"error","error":error.body})]);
        }
        self.ready(Err(error));
    }

    fn ready(&mut self, result: Result<ResponseReady, ApiError>) {
        if let Some(sender) = self.ready.take() {
            let _ = sender.send(result);
        }
    }
}

impl Drop for ActiveResponse {
    fn drop(&mut self) {
        self.tasks
            .active
            .lock()
            .expect("response task lock poisoned")
            .remove(&self.id);
    }
}

/// Builds a public response checkpoint with external tool names restored.
fn response_value(
    meta: &ResponseMeta,
    id: &str,
    created_at: u64,
    output: &[super::super::types::ResponseOutputItem],
    status: ResponseItemStatus,
    usage: Option<super::super::types::ResponseUsage>,
) -> Result<Value, ApiError> {
    let mut value = serde_json::to_value(build_response(
        meta,
        id,
        created_at,
        output.to_vec(),
        status,
        usage,
    ))
    .expect("response serialization");
    super::super::restore_tools(&mut value, &meta.tool_names)?;
    Ok(value)
}
