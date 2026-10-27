// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Serves Draft peer connections and diagnostics on the model-server HTTP listener.

use std::time::{SystemTime, UNIX_EPOCH};

use axum::extract::State;
use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use axum::routing::{any, get, post};
use axum::{Json, Router};
use foretoken_model_protocol::{RuntimeMetadataResponse, TelemetryResponse};
use prometheus_client::encoding::text::encode;
use prometheus_client::metrics::gauge::Gauge;
use prometheus_client::registry::Registry;
use serde::de::DeserializeOwned;
use serde_json::{Value, json};
use tracing::warn;

use super::requests::PeerCommand;
use super::{DraftClient, DraftStatus, PEER_STATUS_PATH};
use crate::runtime_cache;

type HttpError = (StatusCode, Json<Value>);

#[derive(Clone)]
struct AppState {
    client: DraftClient,
    metadata: RuntimeMetadataResponse,
    cache: Option<runtime_cache::Config>,
}

impl DraftClient {
    /// Builds peer ingress and group diagnostics for the native Draft supervisor.
    pub fn router(
        &self,
        metadata: RuntimeMetadataResponse,
        cache: Option<runtime_cache::Config>,
    ) -> Router {
        Router::new()
            .route("/healthz", get(healthz))
            .route("/readyz", get(healthz))
            .route("/metrics", get(metrics))
            .route("/v1/internal/metadata", get(metadata_response))
            .route("/v1/internal/telemetry", get(telemetry))
            .route("/v1/internal/admission/close", post(close_admission))
            .route(PEER_STATUS_PATH, get(peer_status))
            .route("/v1/internal/draft/peers", any(peer_connection))
            .with_state(AppState {
                client: self.clone(),
                metadata,
                cache,
            })
            .layer(axum::extract::DefaultBodyLimit::disable())
    }
}

impl DraftStatus {
    fn telemetry(self) -> TelemetryResponse {
        TelemetryResponse {
            version: 3,
            collected_at_unix_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .expect("system clock predates Unix epoch")
                .as_millis() as u64,
            accepting: self.accepting,
            running_requests: self.active_sessions + self.in_flight_batches,
            request_cost: Default::default(),
            data_parallel_ranks: Vec::new(),
            max_concurrent_requests: None,
            scheduler_running_requests: None,
            scheduler_waiting_requests: None,
            kv_cache_usage: None,
            prompt_tokens_total: None,
            generation_tokens_total: None,
            ttft_seconds: Default::default(),
            tpot_seconds: Default::default(),
            e2e_seconds: Default::default(),
        }
    }
}

async fn healthz(State(state): State<AppState>) -> StatusCode {
    if state.client.healthy() {
        StatusCode::OK
    } else {
        StatusCode::SERVICE_UNAVAILABLE
    }
}

async fn metadata_response(State(state): State<AppState>) -> Json<RuntimeMetadataResponse> {
    Json(state.metadata)
}

/// Publishes resolved native requirements to Target startup without duplicating model inference rules.
async fn peer_status(State(state): State<AppState>) -> Result<Json<DraftStatus>, StatusCode> {
    state
        .client
        .status()
        .map(Json)
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)
}

async fn telemetry(State(state): State<AppState>) -> Result<Json<TelemetryResponse>, StatusCode> {
    state
        .client
        .status()
        .map(|status| Json(status.telemetry()))
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)
}

async fn close_admission(
    State(state): State<AppState>,
) -> Result<Json<TelemetryResponse>, StatusCode> {
    state
        .client
        .drain()
        .map(|status| Json(status.telemetry()))
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)
}

/// Publishes retained session occupancy and the same cache observations used by generation roles.
async fn metrics(State(state): State<AppState>) -> Result<Response, StatusCode> {
    let status = state
        .client
        .status()
        .map_err(|_| StatusCode::SERVICE_UNAVAILABLE)?;
    let mut registry = Registry::default();
    let sessions = Gauge::<u64, std::sync::atomic::AtomicU64>::default();
    sessions.set(status.active_sessions);
    let batches = Gauge::<u64, std::sync::atomic::AtomicU64>::default();
    batches.set(status.in_flight_batches);
    registry.register(
        "foretoken_draft_active_sessions",
        "Native requests retained across proposal rounds",
        sessions,
    );
    registry.register(
        "foretoken_draft_in_flight_batches",
        "Proposal batches whose compute or transfer has not completed",
        batches,
    );
    if let Some(cache) = state.cache {
        registry.register_collector(Box::new(cache));
    }
    let mut output = String::new();
    encode(&mut output, &registry).map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    Ok((
        [(
            header::CONTENT_TYPE,
            "application/openmetrics-text; version=1.0.0; charset=utf-8",
        )],
        output,
    )
        .into_response())
}

/// Uses one bidirectional connection for peer ownership and ordered native proposal turns.
async fn peer_connection(
    State(state): State<AppState>,
    upgrade: WebSocketUpgrade,
) -> Result<Response, HttpError> {
    if !state.client.accepting() {
        return Err((
            StatusCode::SERVICE_UNAVAILABLE,
            Json(json!({"error": "Draft is draining"})),
        ));
    }
    Ok(upgrade
        .max_message_size(usize::MAX)
        .max_frame_size(usize::MAX)
        .on_upgrade(move |mut socket| async move {
            if let Err(error) = serve_peer(&state.client, &mut socket).await {
                warn!(%error, "Draft peer connection failed");
                let error = json!({"error": error.to_string()}).to_string();
                let _ = socket.send(Message::Text(error.into())).await;
            }
            let _ = socket.send(Message::Close(None)).await;
        }))
}

/// Keeps one native peer alive until explicit release, connection loss or runtime shutdown.
async fn serve_peer(client: &DraftClient, socket: &mut WebSocket) -> Result<(), String> {
    let spec = tokio::select! {
        received = receive_peer(socket) => received?,
        () = client.0.health.wait_stopped() => return Ok(()),
    };
    let (connection, opened) = client.open(spec).await?;
    socket
        .send(Message::Text(opened.to_string().into()))
        .await
        .map_err(|error| error.to_string())?;
    loop {
        let command: PeerCommand = tokio::select! {
            received = receive_peer(socket) => received?,
            () = client.0.health.wait_stopped() => return Ok(()),
        };
        let closing = matches!(command, PeerCommand::Close);
        let peer = connection.peer.clone();
        let operation = async {
            match command {
                PeerCommand::Connect => {
                    client
                        .operate(peer, |client, peer| async move {
                            client
                                .rpc::<(), _>("dt_connect_peer", (&peer.spec.peer_id,))
                                .await
                        })
                        .await?;
                    Ok("{}".to_owned())
                }
                PeerCommand::Prepare(batch) => {
                    client
                        .operate(peer, |client, peer| async move {
                            peer.prepare(&client, batch).await
                        })
                        .await?;
                    Ok("{}".to_owned())
                }
                PeerCommand::Propose(batch) => {
                    let proposal = client
                        .operate(peer, |client, peer| async move {
                            peer.propose(&client, batch).await
                        })
                        .await?;
                    serde_json::to_string(&proposal).map_err(|error| error.to_string())
                }
                PeerCommand::Finish(request) => {
                    client
                        .operate(peer, |client, peer| async move {
                            peer.finish(&client, Some(&request.request_ids)).await
                        })
                        .await?;
                    Ok("{}".to_owned())
                }
                PeerCommand::Close => {
                    client.close_peer(peer).await?;
                    Ok("{}".to_owned())
                }
            }
        };
        // Continue observing disconnects while native work owns registered buffers.
        // Cancellation leaves that work joined by its existing operation owner.
        let response = tokio::select! {
            result = operation => result?,
            received = receive_peer::<PeerCommand>(socket) => {
                received?;
                return Err("Target sent another command before its previous turn completed".into());
            },
            () = client.0.health.wait_stopped() => return Ok(()),
        };
        socket
            .send(Message::Text(response.into()))
            .await
            .map_err(|error| error.to_string())?;
        if closing {
            return Ok(());
        }
    }
}

/// Decodes one control message while the WebSocket implementation handles its own framing.
async fn receive_peer<T: DeserializeOwned>(socket: &mut WebSocket) -> Result<T, String> {
    loop {
        let message = socket
            .recv()
            .await
            .ok_or("Target control connection closed")?
            .map_err(|error| error.to_string())?;
        match message {
            Message::Ping(_) | Message::Pong(_) => continue,
            Message::Close(_) => {
                return Err("Target control connection closed".into());
            }
            _ => {
                return serde_json::from_slice(&message.into_data())
                    .map_err(|error| error.to_string());
            }
        }
    }
}
