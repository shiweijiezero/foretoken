// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Activates the selected application and dispatches preparation or Omni video serving.

use foretoken_model_server::backend::vllmomni;
use foretoken_model_server::engine::python_executable;
use foretoken_model_server::model_cache::preparation;

/// Dispatches preparation or transfers the full video-serving lifetime to the Omni adapter.
#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    foretoken_artifacts::source::activate("foretoken-model-server")?;
    vllm_tracing::init_tracing("ForetokenOmniModelServer");
    let python = python_executable();
    match std::env::args().nth(1).as_deref() {
        Some("prepare") => {
            preparation::prepare_job(&foretoken_artifacts::ModelArtifacts::from_env()?, &python)
                .await?;
            Ok(())
        }
        Some("resolve") => {
            preparation::resolve(&foretoken_artifacts::ModelArtifacts::from_env()?, &python)
                .await?;
            Ok(())
        }
        _ => vllmomni::run().await,
    }
}
