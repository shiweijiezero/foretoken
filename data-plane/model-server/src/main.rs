// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Dispatches model preparation, cache collection, and execution-group serving.

mod serving;

use foretoken_model_server::engine;
use foretoken_model_server::model_cache::{collection, preparation};

/// Activate application code and dispatch the command whose owner retains its full resource lifetime.
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    foretoken_artifacts::source::activate("foretoken-model-server")?;
    vllm_tracing::init_tracing("ForetokenModelServer");

    match std::env::args().nth(1).as_deref() {
        Some("prepare") => {
            preparation::prepare_job(
                &foretoken_artifacts::ModelArtifacts::from_env()?,
                &engine::python_executable(),
            )
            .await?;
            Ok(())
        }
        Some("resolve") => {
            preparation::resolve(
                &foretoken_artifacts::ModelArtifacts::from_env()?,
                &engine::python_executable(),
            )
            .await?;
            Ok(())
        }
        Some("collect-cache") => {
            println!("{}", collection::collect().await?);
            Ok(())
        }
        _ => serving::run().await,
    }
}
