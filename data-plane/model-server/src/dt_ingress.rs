// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Applies the model-server's admission and execution ownership to Python DT roles.

use std::io;
use std::sync::Arc;
use std::time::Duration;

use axum::Router;
use axum::body::{Body, to_bytes};
use axum::extract::{Request, State};
use axum::http::{Method, StatusCode, header};
use axum::response::{IntoResponse, Response};
use bytes::Bytes;
use foretoken_request_ledger::{Outcome, RequestLedger, ReservationRef};
use futures::StreamExt;
use serde_json::Value;

use crate::api::{AdmissionPermit, ExecutionStore, ReservationEnd, RuntimeHealth, finish_shared};

#[derive(Clone)]
struct Ingress {
    endpoint: String,
    client: reqwest::Client,
    health: Arc<RuntimeHealth>,
    ledger: Option<ExecutionStore>,
    body_limit: usize,
}

/// Builds the supervised DT role ingress, retaining permits until Python confirms session cleanup.
/// Bootstrap must bind the public listener before registering the supplied backend epoch.
pub fn router(
    endpoint: String,
    health: Arc<RuntimeHealth>,
    ledger: Option<(Arc<RequestLedger>, String, u64)>,
    body_limit: usize,
    profiling: Option<crate::profiling::Handle>,
) -> io::Result<Router> {
    let state = Ingress {
        endpoint,
        client: reqwest::Client::builder()
            .no_proxy()
            .build()
            .map_err(io::Error::other)?,
        health,
        ledger: ledger.map(|(ledger, pod_uid, epoch)| ExecutionStore {
            ledger,
            pod_uid,
            epoch,
        }),
        body_limit,
    };
    Ok(Router::new()
        .fallback(forward)
        .with_state(state)
        .merge(crate::profiling::router(profiling)))
}

// Admission lives at the Rust listener; Python remains the owner of sessions and engine work.
async fn forward(State(state): State<Ingress>, request: Request) -> Response {
    let path = request.uri().path().to_owned();
    if request.method() == Method::GET {
        match path.as_str() {
            "/v1/internal/admission" => {
                return axum::Json(state.health.instance_admission_status()).into_response();
            }
            "/v1/internal/admission/ready" | "/readyz/instance-admission" => {
                return if state.health.ready()
                    && state
                        .health
                        .instance_admission_status()
                        .active_generation
                        .is_some()
                {
                    StatusCode::OK
                } else {
                    StatusCode::SERVICE_UNAVAILABLE
                }
                .into_response();
            }
            _ => {}
        }
    }
    let opening = request.method() == Method::POST
        && matches!(path.as_str(), "/generate" | "/sessions" | "/resume");
    let closing = request.method() == Method::POST
        && matches!(path.as_str(), "/v1/internal/admission/close" | "/drain");
    if closing {
        state.health.set_accepting(false);
    }
    let url = format!(
        "{}{}",
        state.endpoint,
        request
            .uri()
            .path_and_query()
            .map_or(path.as_str(), |value| value.as_str())
    );
    let method = request.method().clone();
    let content_type = request.headers().get(header::CONTENT_TYPE).cloned();
    let body = match to_bytes(request.into_body(), state.body_limit).await {
        Ok(body) => body,
        Err(_) => return StatusCode::PAYLOAD_TOO_LARGE.into_response(),
    };
    if opening {
        let mut input: Value = match serde_json::from_slice(&body) {
            Ok(input) => input,
            Err(_) => return StatusCode::BAD_REQUEST.into_response(),
        };
        let generation = if path == "/resume" {
            input.get_mut("generation")
        } else {
            Some(&mut input)
        };
        let Some(generation) = generation.and_then(Value::as_object_mut) else {
            return StatusCode::BAD_REQUEST.into_response();
        };
        let reservation: Option<ReservationRef> = match generation.remove("reservation") {
            Some(value) => match serde_json::from_value(value) {
                Ok(reference) => reference,
                Err(_) => return StatusCode::BAD_REQUEST.into_response(),
            },
            None => None,
        };
        let shared = match reservation {
            Some(reference) => match state.ledger.clone() {
                Some(store) => Some((store, reference)),
                None => return StatusCode::SERVICE_UNAVAILABLE.into_response(),
            },
            None => None,
        };
        let permit = match state.health.try_admit() {
            Ok(permit) => permit,
            Err(crate::api::AdmissionRejection::Busy) => {
                return crate::api::ApiError::Busy.into_response();
            }
            Err(crate::api::AdmissionRejection::Closed) => {
                return (
                    [(header::CONTENT_TYPE, "application/x-ndjson")],
                    Body::from("{\"event\":\"admission_closed\"}\n"),
                )
                    .into_response();
            }
        };
        let (reply, received) = tokio::sync::oneshot::channel();
        // Submission cannot be abandoned by a dropped HTTP handler after ledger acceptance.
        tokio::spawn(run_session(state, url, input, shared, permit, reply));
        return received
            .await
            .unwrap_or_else(|_| StatusCode::BAD_GATEWAY.into_response());
    }
    let mut request = state.client.request(method, url).body(body);
    if let Some(content_type) = content_type {
        request = request.header(header::CONTENT_TYPE, content_type);
    }
    let response = match request.send().await {
        Ok(response) => response,
        Err(_) => return StatusCode::BAD_GATEWAY.into_response(),
    };
    let status = response.status();
    let content_type = response.headers().get(header::CONTENT_TYPE).cloned();
    if status.is_success()
        && matches!(
            path.as_str(),
            "/status" | "/v1/internal/telemetry" | "/v1/internal/admission/close" | "/drain"
        )
    {
        let mut value: Value = match response.json().await {
            Ok(value) => value,
            Err(_) => return StatusCode::BAD_GATEWAY.into_response(),
        };
        value["accepting"] = state.health.accepting().into();
        if path != "/status" && path != "/drain" {
            value["running_requests"] = state.health.running_requests().into();
            value["max_concurrent_requests"] = state
                .health
                .instance_admission_status()
                .max_concurrent_requests
                .into();
        }
        return axum::Json(value).into_response();
    }
    let mut output = (status, Body::from_stream(response.bytes_stream())).into_response();
    if let Some(content_type) = content_type {
        output
            .headers_mut()
            .insert(header::CONTENT_TYPE, content_type);
    }
    output
}

// The stream task owns the slot independently of both HTTP handlers and shared-store availability.
async fn run_session(
    state: Ingress,
    url: String,
    input: Value,
    shared: Option<(ExecutionStore, ReservationRef)>,
    permit: AdmissionPermit,
    mut reply: tokio::sync::oneshot::Sender<Response>,
) {
    let owner = uuid::Uuid::new_v4().to_string();
    if let Some((store, reference)) = &shared {
        loop {
            match store
                .ledger
                .accept(reference, &owner, &store.pod_uid, store.epoch)
                .await
            {
                Ok(Outcome::Applied) => break,
                Ok(Outcome::BackendUnavailable) => {
                    let _ = reply.send(crate::api::ApiError::AdmissionUnavailable.into_response());
                    return;
                }
                Ok(_) => {
                    drop(permit);
                    finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
                    let _ = reply.send(StatusCode::CONFLICT.into_response());
                    return;
                }
                Err(_) if reply.is_closed() => {
                    drop(permit);
                    finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
                    return;
                }
                Err(_) => tokio::time::sleep(Duration::from_secs(1)).await,
            }
        }
    }
    if reply.is_closed() {
        drop(permit);
        finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
        return;
    }
    let draft = url.ends_with("/sessions");
    let response = tokio::select! {
        response = state.client.post(url).json(&input).send() => response,
        () = reply.closed() => {
            state.health.fail_execution();
            state.health.wait_stopped().await;
            drop(permit);
            finish_shared(&shared, &owner, ReservationEnd::RequestTerminated).await;
            return;
        }
        () = state.health.wait_stopped() => {
            drop(permit);
            finish_shared(&shared, &owner, ReservationEnd::RequestTerminated).await;
            return;
        }
    };
    let response = match response {
        Ok(response) if response.status().is_success() => response,
        Ok(response) if response.status().is_client_error() => {
            let status = response.status();
            drop(permit);
            finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
            let _ = reply.send(status.into_response());
            return;
        }
        _ => {
            let _ = reply.send(StatusCode::BAD_GATEWAY.into_response());
            state.health.fail_execution();
            state.health.wait_stopped().await;
            drop(permit);
            finish_shared(&shared, &owner, ReservationEnd::RequestTerminated).await;
            return;
        }
    };
    let (sender, mut receiver) = tokio::sync::mpsc::channel::<Result<Bytes, io::Error>>(1);
    let body =
        async_stream::stream! { while let Some(chunk) = receiver.recv().await { yield chunk; } };
    let _ = reply.send(
        (
            [(header::CONTENT_TYPE, "application/x-ndjson")],
            Body::from_stream(body),
        )
            .into_response(),
    );
    let mut stream = response.bytes_stream();
    let mut pending = Vec::new();
    let mut session = None;
    let mut terminal = None;
    let mut refused = false;
    let mut clean_eof = false;
    {
        let cancellation = async {
            loop {
                tokio::select! {
                    () = sender.closed() => return,
                    () = tokio::time::sleep(Duration::from_millis(250)) => {},
                }
                if let Some((store, reference)) = &shared
                    && matches!(store.ledger.cancelled(reference).await, Ok(true))
                {
                    return;
                }
            }
        };
        tokio::pin!(cancellation);
        'relay: loop {
            let chunk = tokio::select! {
                () = &mut cancellation => break,
                () = state.health.wait_stopped() => break,
                chunk = stream.next() => chunk,
            };
            let chunk = match chunk {
                Some(Ok(chunk)) => chunk,
                None => {
                    clean_eof = pending.is_empty();
                    break;
                }
                Some(Err(_)) => break,
            };
            pending.extend_from_slice(&chunk);
            while let Some(end) = pending.iter().position(|byte| *byte == b'\n') {
                let line: Vec<_> = pending.drain(..=end).collect();
                let event: Value = match serde_json::from_slice(&line) {
                    Ok(event) => event,
                    Err(_) => break 'relay,
                };
                if event["event"] == "opened" {
                    session = event["session_id"].as_str().map(str::to_owned);
                }
                refused |= event["event"] == "admission_closed";
                // Deliver final output only after the Python generator's finally block has completed.
                if event["finished"] == true || refused {
                    terminal = Some(Bytes::from(line));
                    continue;
                }
                tokio::select! {
                    () = &mut cancellation => break 'relay,
                    () = state.health.wait_stopped() => break 'relay,
                    result = sender.send(Ok(Bytes::from(line))) => if result.is_err() { break 'relay; },
                }
            }
        }
    }
    clean_eof &= draft || terminal.is_some() || refused;
    if !clean_eof {
        // DELETE awaits role-owned abort completion. Without definite evidence, stop the process.
        let cleanup = async {
            let id = session.as_ref().ok_or(())?;
            state
                .client
                .delete(format!("{}/sessions/{id}", state.endpoint))
                .send()
                .await
                .map_err(|_| ())?
                .error_for_status()
                .map_err(|_| ())?;
            Ok::<_, ()>(())
        };
        let result = tokio::select! {
            result = cleanup => result,
            () = state.health.wait_stopped() => Ok(()),
        };
        if result.is_err() {
            state.health.fail_execution();
            state.health.wait_stopped().await;
        }
    }
    drop(permit);
    let end = if !clean_eof {
        ReservationEnd::RequestTerminated
    } else if refused {
        ReservationEnd::AdmissionRefused
    } else {
        ReservationEnd::StageTerminated
    };
    // A definite refusal must return ownership before Frontend can retry admission.
    let handoff = clean_eof
        && (refused
            || shared
                .as_ref()
                .is_some_and(|(_, reference)| !reference.final_stage));
    if handoff {
        finish_shared(&shared, &owner, end).await;
    }
    if clean_eof && let Some(terminal) = terminal {
        let _ = sender.send(Ok(terminal)).await;
    }
    if !clean_eof {
        let _ = sender
            .send(Err(io::Error::other(
                "DT session terminated without final output",
            )))
            .await;
    }
    drop(sender);
    if !handoff {
        finish_shared(&shared, &owner, end).await;
    }
}
