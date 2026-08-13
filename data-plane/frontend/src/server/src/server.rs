// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! HTTP server lifecycle: bind, serve, and graceful shutdown.

use tokio::net::TcpListener;

use crate::config::ServerConfig;
use crate::routes::build_router;

/// Bind the configured address and serve the frontend until shutdown.
pub async fn serve(config: ServerConfig) -> std::io::Result<()> {
    let listener = TcpListener::bind(config.socket_addr()).await?;
    serve_listener(listener, shutdown_signal()).await
}

/// Serve on an already-bound listener until `shutdown` resolves.
///
/// The caller owns the listener, so tests can bind an ephemeral port and learn
/// the real address before starting the server, and pass a one-shot channel as
/// the shutdown signal.
pub async fn serve_listener(
    listener: TcpListener,
    shutdown: impl std::future::Future<Output = ()> + Send + 'static,
) -> std::io::Result<()> {
    let addr = listener.local_addr()?;
    tracing::info!(%addr, "foretoken frontend listening");

    let app = build_router();
    axum::serve(listener, app)
        .with_graceful_shutdown(shutdown)
        .await
}

/// Resolves on SIGINT (Ctrl-C) or SIGTERM.
async fn shutdown_signal() {
    let ctrl_c = async {
        tokio::signal::ctrl_c()
            .await
            .expect("failed to install Ctrl-C handler");
    };

    #[cfg(unix)]
    let terminate = async {
        tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())
            .expect("failed to install SIGTERM handler")
            .recv()
            .await;
    };

    #[cfg(not(unix))]
    let terminate = std::future::pending::<()>();

    tokio::select! {
        _ = ctrl_c => {},
        _ = terminate => {},
    }

    tracing::info!("shutdown signal received, draining connections");
}
