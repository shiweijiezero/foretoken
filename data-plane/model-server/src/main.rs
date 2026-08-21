// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Starts a managed local EngineCore child and serves the restricted internal API.

use std::future::IntoFuture;
use std::sync::Arc;
use std::time::Instant;

use foretoken_model_protocol::{RuntimeMetadataResponse, RuntimeModelIdentity};
use foretoken_model_server::api::{AppState, RuntimeHealth, router};
use foretoken_model_server::backend::VllmBackend;
use foretoken_model_server::config::RuntimeConfig;
use foretoken_model_server::kv_event_adapter::KvEventAdapter;
use foretoken_model_server::runtime_transport::LOOPBACK_HOST;
use tokio::net::TcpListener;
use tokio::sync::Notify;
use tracing::{error, info, warn};
use vllm_engine_core_client::{EngineCoreClient, EngineCoreClientConfig, TransportMode};
use vllm_llm::Llm;
use vllm_managed_engine::{ManagedEngineHandle, allocate_handshake_port};

const KV_KEY_PATH_ENV: &str = "FORETOKEN_KV_INDEX_KEY_PATH";
const KV_SCOPE_ENV: &str = "FORETOKEN_KV_SCOPE_ID";
const MODEL_GROUP_UID_ENV: &str = "FORETOKEN_MODEL_GROUP_UID";

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    vllm_tracing::init_tracing("ForetokenModelServer");

    // Resolve the controller-owned launch plan before starting any engine or network task.
    let config = RuntimeConfig::from_env().map_err(std::io::Error::other)?;

    // Resolve optional KV projection state now; connect only after the engine publisher is ready.
    let kv_events = match kv_event_adapter(&config) {
        Ok(adapter) => Some(adapter),
        Err(error) => {
            warn!(%error, "KV index configuration is unavailable; prefix scoring is disabled");
            None
        }
    };

    // The model-server owns one managed engine and the loopback handshake used by its client.
    let handshake_port = allocate_handshake_port(LOOPBACK_HOST)?;
    let engine = ManagedEngineHandle::spawn(
        config
            .launch
            .managed_engine(handshake_port)
            .map_err(std::io::Error::other)?,
    )
    .await?;
    let health = Arc::new(RuntimeHealth::new());
    health.set_process_alive(true);

    let client_config = EngineCoreClientConfig {
        transport_mode: TransportMode::HandshakeOwner {
            handshake_address: format!("tcp://{LOOPBACK_HOST}:{handshake_port}"),
            advertised_host: LOOPBACK_HOST.into(),
            engine_count: config.launch.parallelism.dp,
            ready_timeout: config.launch.startup_timeout(),
            local_input_address: None,
            local_output_address: None,
        },
        coordinator_mode: None,
        model_name: config.launch.artifacts.model.clone(),
        client_index: 0,
    };
    let client = tokio::select! {
        result = EngineCoreClient::connect(client_config) => result.map_err(|error| std::io::Error::other(format!("could not connect to EngineCore: {error}"))),
        status = engine.wait_for_exit() => Err(std::io::Error::other(format!("managed EngineCore exited during startup: {status}"))),
    };
    let client = match client {
        Ok(client) => client,
        Err(error) => {
            let _ = engine.shutdown(config.launch.drain_timeout()).await;
            return Err(error.into());
        }
    };

    let mut client_health = client.subscribe_health();
    let max_concurrent_requests = client
        .ready_responses()
        .into_iter()
        .try_fold(0_u64, |total, ready| total.checked_add(ready.max_num_seqs))
        .ok_or_else(|| std::io::Error::other("EngineCore max_num_seqs sum overflowed"))?;
    let metadata = RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: config.launch.artifacts.model.clone(),
            revision: config.launch.artifacts.revision.clone(),
        },
        model_dtype: client.model_dtype(),
        effective_max_model_len: client.max_model_len(),
        ec_transfer: config.launch.ec.runtime_metadata(),
        capabilities: if config.launch.ec.enabled() {
            ["ec_transfer".into()].into_iter().collect()
        } else {
            Default::default()
        },
    };
    if !*client_health.borrow() {
        let reason = client.health_error().map_or_else(
            || "EngineCore client became unhealthy during startup".to_string(),
            |error| format!("EngineCore client became unhealthy during startup: {error}"),
        );
        let _ = client.shutdown().await;
        let _ = engine.shutdown(config.launch.drain_timeout()).await;
        health.set_process_alive(false);
        return Err(std::io::Error::other(reason).into());
    }
    let kv_events = if let Some(adapter) = kv_events {
        let (ready_tx, ready_rx) = tokio::sync::oneshot::channel();
        tokio::spawn(adapter.clone().serve(ready_tx));
        if ready_rx.await == Ok(true) {
            Some(adapter)
        } else {
            warn!("KV event subscriber is unavailable; prefix scoring is disabled");
            None
        }
    } else {
        None
    };
    health.set_client_healthy(true);
    health.set_accepting(true);
    let backend = Arc::new(VllmBackend::new(Llm::new(client), max_concurrent_requests));

    // Expose only the restricted group-local API after EngineCore is connected and healthy.
    let listener = match TcpListener::bind(config.listen_address).await {
        Ok(listener) => listener,
        Err(error) => {
            health.set_accepting(false);
            health.set_client_healthy(false);
            let _ = backend.shutdown().await;
            let _ = engine.shutdown(config.launch.drain_timeout()).await;
            return Err(error.into());
        }
    };
    let shutdown = Arc::new(Notify::new());
    let server_shutdown = shutdown.clone();
    let mut app_state = AppState::new(backend.clone(), health.clone(), metadata);
    if let Some(kv_events) = kv_events {
        app_state = app_state.with_kv_events(kv_events);
    }
    let mut server = Box::pin(
        axum::serve(
            listener,
            router(
                app_state,
                config.launch.internal_generate_request_body_limit_bytes,
            ),
        )
        .with_graceful_shutdown(async move { server_shutdown.notified().await })
        .into_future(),
    );

    // Signals, client health, child exit, and server failure converge on one shutdown path.
    enum Stop {
        Signal,
        ClientUnhealthy(String),
        ChildExited(String),
        Server(String),
    }
    let stop = tokio::select! {
        () = shutdown_signal() => Stop::Signal,
        changed = client_health.changed() => {
            if changed.is_err() || !*client_health.borrow() {
                Stop::ClientUnhealthy("EngineCore client became unhealthy".into())
            } else {
                unreachable!("EngineCore health only transitions to unhealthy")
            }
        },
        status = engine.wait_for_exit() => Stop::ChildExited(format!("managed EngineCore exited unexpectedly: {status}")),
        result = &mut server => Stop::Server(match result {
            Ok(()) => "HTTP server stopped unexpectedly".into(),
            Err(error) => format!("HTTP server failed: {error}"),
        }),
    };
    match &stop {
        Stop::Signal => info!("received shutdown signal"),
        Stop::ClientUnhealthy(reason) | Stop::ChildExited(reason) | Stop::Server(reason) => {
            warn!(%reason)
        }
    }

    // Stop new admission before draining HTTP handlers, the client, and finally the child process.
    health.set_accepting(false);
    health.set_client_healthy(false);
    shutdown.notify_waiters();
    let deadline = Instant::now() + config.launch.drain_timeout();
    if !matches!(&stop, Stop::Server(_)) {
        match tokio::time::timeout(config.launch.drain_timeout(), server.as_mut()).await {
            Ok(Ok(())) => {}
            Ok(Err(error)) => error!(%error, "HTTP server failed while draining"),
            Err(_) => {
                warn!("HTTP handlers did not drain before deadline");
                drop(server);
            }
        }
    }
    if let Err(error) = backend.shutdown().await {
        warn!(%error, "could not shut down EngineCore client cleanly");
    }
    let remaining = deadline.saturating_duration_since(Instant::now());
    if let Err(error) = engine.shutdown(remaining).await {
        warn!(%error, "could not shut down managed EngineCore cleanly");
    }
    health.set_process_alive(false);

    match stop {
        Stop::Signal => Ok(()),
        Stop::ClientUnhealthy(reason) | Stop::ChildExited(reason) | Stop::Server(reason) => {
            Err(std::io::Error::other(reason).into())
        }
    }
}

fn kv_event_adapter(
    config: &RuntimeConfig,
) -> Result<Arc<KvEventAdapter>, Box<dyn std::error::Error>> {
    let bytes = std::fs::read(std::env::var(KV_KEY_PATH_ENV)?)?;
    let key: [u8; 32] = bytes
        .as_slice()
        .try_into()
        .map_err(|_| "KV index key must be exactly 32 bytes")?;
    let scope_id = required_env(KV_SCOPE_ENV)?;
    let model_group_id = required_env(MODEL_GROUP_UID_ENV)?;
    Ok(KvEventAdapter::new(
        key,
        scope_id,
        model_group_id,
        config.launch.artifacts.revision.clone(),
        config.launch.parallelism.dp.try_into()?,
    ))
}

fn required_env(name: &str) -> Result<String, Box<dyn std::error::Error>> {
    std::env::var(name)
        .ok()
        .filter(|value| !value.is_empty())
        .ok_or_else(|| format!("{name} must be set by the ModelGroup controller").into())
}

async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("installing Ctrl-C signal handler must succeed");
    };
    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("installing SIGTERM signal handler must succeed")
            .recv()
            .await;
    };
    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();
    tokio::select! { () = ctrl_c => {}, () = terminate => {} }
}
