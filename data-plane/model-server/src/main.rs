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

    let command = std::env::args().nth(1);
    if matches!(command.as_deref(), Some("prepare" | "resolve")) {
        let plan = std::env::var("FORETOKEN_VLLM_LAUNCH_PLAN")
            .ok()
            .map(|input| foretoken_model_server::backend::vllm::launch::LaunchPlanV1::parse(&input))
            .transpose()?;
        let artifacts = match &plan {
            Some(plan) => plan.artifacts.clone(),
            None => foretoken_artifacts::ModelArtifacts::from_env()?,
        };
        let draft = plan
            .as_ref()
            .and_then(|plan| plan.dt.as_ref())
            .and_then(|dt| dt.draft_artifacts.as_ref());
        let python = engine::python_executable();
        return match command.as_deref() {
            Some("prepare") => preparation::prepare_job(&artifacts, draft, &python)
                .await
                .map_err(Into::into),
            _ => preparation::resolve(&artifacts, draft, &python)
                .await
                .map_err(Into::into),
        };
    }
    match command.as_deref() {
        Some("collect-cache") => {
            println!("{}", collection::collect().await?);
            Ok(())
        }
        _ => serving::run().await,
    }
}
