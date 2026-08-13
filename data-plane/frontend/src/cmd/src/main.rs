// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Foretoken frontend entry point.

use clap::Parser;

use foretoken_server::{ServerConfig, serve};

/// OpenAI-compatible HTTP frontend for the Foretoken data plane.
#[derive(Debug, Parser)]
struct Cli {
    /// Address to bind to.
    #[arg(long, default_value = "0.0.0.0")]
    host: String,

    /// Port to bind to.
    #[arg(long, default_value_t = 8000)]
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

    let config = ServerConfig {
        host: cli.host,
        port: cli.port,
    };

    serve(config).await
}
