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
        Some(command @ ("prepare" | "resolve")) => {
            let (artifacts, draft) = if std::env::var_os(foretoken_artifacts::MODEL_ARTIFACTS_ENV)
                .is_some()
            {
                (foretoken_artifacts::ModelArtifacts::from_env()?, None)
            } else {
                let plan = foretoken_model_server::backend::vllm::launch::LaunchPlanV1::from_env()?;
                (plan.artifacts, plan.dt.and_then(|dt| dt.draft_artifacts))
            };
            let python = engine::python_executable();
            if command == "prepare" {
                preparation::prepare_job(&artifacts, draft.as_ref(), &python).await?;
            } else {
                preparation::resolve(&artifacts, draft.as_ref(), &python).await?;
            }
            Ok(())
        }
        Some("collect-cache") => {
            println!("{}", collection::collect().await?);
            Ok(())
        }
        _ => serving::run().await,
    }
}
