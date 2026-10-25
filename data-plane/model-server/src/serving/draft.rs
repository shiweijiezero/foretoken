// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Supervises a native Draft process while peer sessions retain their request and transfer lifetimes.

use std::future::IntoFuture;
use std::io;
use std::sync::Arc;
use std::time::{Duration, Instant};

use axum::serve::ListenerExt;
use foretoken_model_protocol::{RuntimeMetadataResponse, RuntimeModelIdentity};
use foretoken_model_server::backend::vllm::config::RuntimeConfig;
use foretoken_model_server::backend::vllm::startup::{self, ConnectedEngine, EngineClient};
use foretoken_model_server::{profiling, runtime_cache, shutdown_signal};
use tokio::net::TcpListener;
use tokio_util::sync::CancellationToken;
use tracing::{info, warn};

enum Stop {
    Signal,
    Engine(String),
    Server(String),
    Cache(String),
    Profiling(String),
    SignalFailure(String),
}

/// Reuses model preparation and process startup, then keeps peer ingress alive through native drain.
pub(super) async fn run(
    config: RuntimeConfig,
    cache: Option<runtime_cache::Config>,
    profiling_config: Option<profiling::Config>,
    mut cache_server: Option<tokio::task::JoinHandle<io::Result<()>>>,
    cache_shutdown: CancellationToken,
) -> Result<(), Box<dyn std::error::Error>> {
    // Keep peer RPC latency independent of TCP delayed acknowledgements.
    let listener = TcpListener::bind(config.listen_address)
        .await?
        .tap_io(|stream| {
            if let Err(error) = stream.set_nodelay(true) {
                warn!(%error, "could not disable Nagle for Draft peer connection");
            }
        });
    let ConnectedEngine {
        engine,
        client,
        cache_mode,
        prepared_tokenizer,
        ..
    } = startup::start(
        &config,
        cache.as_ref(),
        profiling_config.as_ref(),
        Instant::now() + config.launch.startup_timeout(),
        &mut cache_server,
    )
    .await?;
    let EngineClient::Draft(client) = client else {
        unreachable!("Draft supervisor received a generation connection");
    };
    let health = client.health();
    health.set_process_alive(true);
    health.set_client_healthy(true);
    health.set_accepting(true);
    let mut profiler = profiling_config
        .filter(|_| cache_mode == runtime_cache::Mode::Persistent)
        .map(|profile| profiling::Supervisor::new(profile, Some(Arc::new(client.clone()))));
    let metadata = RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: config.launch.artifacts.model.clone(),
            revision: config.launch.artifacts.revision.clone(),
        },
        model_dtype: None,
        effective_max_model_len: 0,
        max_logprobs: None,
        ec_transfer: None,
        prepared_tokenizer,
        capabilities: Default::default(),
    };
    let app = client.router(metadata, cache).merge(profiling::router(
        profiler.as_ref().map(|profiler| profiler.handle()),
    ));
    let shutdown = CancellationToken::new();
    let mut server = Box::pin(
        axum::serve(listener, app)
            .with_graceful_shutdown(shutdown.clone().cancelled_owned())
            .into_future(),
    );
    info!(address = %config.listen_address, "Draft peer service ready");
    let mut stop = tokio::select! {
        signal = shutdown_signal() => match signal {
            Ok(()) => Stop::Signal,
            Err(error) => Stop::SignalFailure(format!("shutdown signal handler failed: {error}")),
        },
        status = engine.wait_for_exit() => Stop::Engine(format!("managed Draft exited: {status}")),
        reason = client.wait_failed() => Stop::Engine(reason),
        result = &mut server => Stop::Server(format!("Draft HTTP server stopped: {result:?}")),
        reason = runtime_cache::wait_for_server(&mut cache_server) => Stop::Cache(reason),
        result = async {
            match &mut profiler {
                Some(profiler) => profiler.run().await,
                None => std::future::pending().await,
            }
        } => Stop::Profiling(result.err().unwrap_or_else(|| "capture supervisor stopped".into())),
    };

    match &stop {
        Stop::Signal => info!("received shutdown signal"),
        Stop::Engine(reason)
        | Stop::Server(reason)
        | Stop::Cache(reason)
        | Stop::Profiling(reason)
        | Stop::SignalFailure(reason) => warn!(%reason),
    }

    // A proposal is not a request lifetime: keep accepting continuation RPCs while native sessions drain.
    health.set_accepting(false);
    if let Some(profiler) = &profiler {
        profiler.handle().close();
    }
    let deadline = Instant::now() + config.launch.drain_timeout();
    let mut server_stopped = matches!(stop, Stop::Server(_));
    let force_stop = matches!(stop, Stop::Engine(_))
        || profiler
            .as_ref()
            .is_some_and(|profiler| profiler.has_native_capture());
    if !force_stop && !server_stopped {
        let drain = async {
            let mut status = client.drain()?;
            while !status.drained() {
                tokio::time::sleep(Duration::from_millis(100)).await;
                status = client.status()?;
            }
            Ok::<_, String>(())
        };
        tokio::select! {
            result = tokio::time::timeout_at(deadline.into(), drain) => match result {
                Ok(Ok(())) => {},
                Ok(Err(error)) => {
                    warn!(%error, "Draft drain observation failed");
                    if matches!(stop, Stop::Signal) {
                        stop = Stop::Engine(format!("Draft drain failed: {error}"));
                    }
                },
                Err(_) => warn!("native Draft sessions did not finish before the drain deadline"),
            },
            status = engine.wait_for_exit() => {
                warn!(%status, "managed Draft exited during drain");
                if matches!(stop, Stop::Signal) {
                    stop = Stop::Engine(format!("managed Draft exited during drain: {status}"));
                }
            },
            result = &mut server => {
                server_stopped = true;
                warn!(?result, "Draft HTTP server stopped during drain");
                if matches!(stop, Stop::Signal) {
                    stop = Stop::Server(format!("Draft HTTP server stopped during drain: {result:?}"));
                }
            },
        }
    }

    // Stop the native owner before releasing response streams that may still reference its transfers.
    client.begin_shutdown();
    engine
        .shutdown(deadline.saturating_duration_since(Instant::now()))
        .await
        .map_err(|cleanup| match &stop {
            Stop::Signal => io::Error::other(cleanup),
            Stop::Engine(reason)
            | Stop::Server(reason)
            | Stop::Cache(reason)
            | Stop::Profiling(reason)
            | Stop::SignalFailure(reason) => {
                io::Error::other(format!("{reason}; runtime cleanup also failed: {cleanup}"))
            }
        })?;
    health.set_process_alive(false);
    health.set_client_healthy(false);
    shutdown.cancel();
    cache_shutdown.cancel();
    if let Some(profiler) = &mut profiler {
        profiler.engine_stopped("runtime terminated").await;
    }
    if !server_stopped {
        match tokio::time::timeout_at(deadline.into(), &mut server).await {
            Ok(Ok(())) => {}
            Ok(Err(error)) => warn!(%error, "Draft HTTP server failed during shutdown"),
            Err(_) => warn!("Draft HTTP shutdown reached the drain deadline"),
        }
    }
    match stop {
        Stop::Signal => Ok(()),
        Stop::Engine(reason)
        | Stop::Server(reason)
        | Stop::Cache(reason)
        | Stop::Profiling(reason)
        | Stop::SignalFailure(reason) => Err(io::Error::other(reason).into()),
    }
}
