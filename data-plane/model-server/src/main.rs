// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Mock model-server entry point.

use std::sync::Arc;

use clap::Parser;

use foretoken_model_server::{MockModelServer, build_mock_router};

/// Mock model-server that echoes fixed tokens over SSE.
#[derive(Debug, Parser)]
struct Cli {
    /// Address to bind to.
    #[arg(long, default_value = "0.0.0.0")]
    host: String,

    /// Port to bind to.
    #[arg(long, default_value_t = 9000)]
    port: u16,
}

#[tokio::main]
async fn main() -> std::io::Result<()> {
    let cli = Cli::parse();

    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "info".into()),
        )
        .init();

    let state = Arc::new(MockModelServer::default());
    let router = build_mock_router(state);
    let listener = tokio::net::TcpListener::bind((cli.host.as_str(), cli.port)).await?;
    let addr = listener.local_addr()?;
    tracing::info!(%addr, "foretoken mock model-server listening");

    axum::serve(listener, router).await
}
