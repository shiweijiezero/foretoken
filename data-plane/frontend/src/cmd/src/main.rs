// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Foretoken frontend entry point.

use std::sync::Arc;

use clap::Parser;

use foretoken_chat::{CharTokenizer, ChatFacade};
use foretoken_server::{AppState, ServerConfig, serve};

/// OpenAI-compatible HTTP frontend for the Foretoken data plane.
#[derive(Debug, Parser)]
struct Cli {
    /// Address to bind to.
    #[arg(long, default_value = "0.0.0.0")]
    host: String,

    /// Port to bind to.
    #[arg(long, default_value_t = 8000)]
    port: u16,

    /// Downstream model-server base URL.
    #[arg(long, default_value = "http://127.0.0.1:9000")]
    model_server: String,
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
        model_server_url: cli.model_server,
    };

    // Placeholder char tokenizer until a real vocabulary is wired in.
    let chat = Arc::new(ChatFacade::new(
        Arc::new(foretoken_chat::vllm::DeepSeekV4Renderer),
        Arc::new(CharTokenizer),
    ));
    let state = AppState {
        chat,
        model_server_url: config.model_server_url.clone(),
    };

    serve(config, state).await
}
