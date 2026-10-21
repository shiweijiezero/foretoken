// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolves the controller's Omni launch contract into a prepared model and native command.

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::process::Command;
use std::time::Instant;

use foretoken_artifacts::{ModelArtifacts, ModelSource};
use serde::Deserialize;
use serde_json::Value;

use crate::engine::{Lifecycle, append_engine_arg};
use crate::model_cache::preparation::{self, PreparationStorage};

const LAUNCH_PLAN_ENV: &str = "FORETOKEN_OMNI_LAUNCH_PLAN";
const LISTEN_ENV: &str = "FORETOKEN_INTERNAL_LISTEN";
const EXECUTABLE_ENV: &str = "FORETOKEN_OMNI_EXECUTABLE";

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub(super) struct LaunchPlan {
    version: u8,
    pub(super) artifacts: ModelArtifacts,
    pub(super) upstream_port: u16,
    pub(super) lifecycle: Lifecycle,
    #[serde(default)]
    engine_args: BTreeMap<String, Value>,
}

impl LaunchPlan {
    /// Resolves startup inputs before the serving owner allocates sockets or an engine process.
    pub(super) fn from_env() -> Result<(Self, SocketAddr), Box<dyn std::error::Error>> {
        let plan: Self = serde_json::from_str(&required_env(LAUNCH_PLAN_ENV)?)?;
        plan.validate()?;
        let listen: SocketAddr = required_env(LISTEN_ENV)?.parse()?;
        if listen.port() == plan.upstream_port {
            return Err("adapter and vLLM-Omni ports must differ".into());
        }
        Ok((plan, listen))
    }

    /// Binds the native command to the fixed publication before startup can allocate accelerators.
    pub(super) async fn prepare_model(
        &self,
        python: &str,
        deadline: Instant,
    ) -> Result<String, Box<dyn std::error::Error>> {
        if self.artifacts.source == ModelSource::Local {
            return Ok(preparation::local_artifact_path(&self.artifacts.model)?);
        }
        let model_root = foretoken_artifacts::model_cache_root()
            .ok_or("FORETOKEN_MODEL_CACHE_ROOT must be set for model preparation")?;
        let state_root = foretoken_artifacts::preparation_root()
            .ok_or("FORETOKEN_MODEL_PREPARATION_ROOT must be set for model preparation")?;
        let storage = PreparationStorage {
            model_root: &model_root,
            state_root: &state_root,
            reusable_roots: None,
            environment: &[],
        };
        let scope = required_env(preparation::PREPARATION_SCOPE_ENV)?;
        Ok(tokio::time::timeout_at(
            deadline.into(),
            preparation::prepare(&self.artifacts, python, &storage, &scope, true),
        )
        .await
        .map_err(|_| "model preparation exceeded the startup deadline")??
        .model
        .display()
        .to_string())
    }

    /// Builds the child command from the prepared model without exposing native flags to HTTP callers.
    pub(super) fn engine_command(
        &self,
        model: &str,
    ) -> Result<Command, Box<dyn std::error::Error>> {
        let executable = std::env::var(EXECUTABLE_ENV).unwrap_or_else(|_| "vllm".into());
        let mut command = Command::new(executable);
        command
            .arg("serve")
            .arg(model)
            .arg("--omni")
            .arg("--host=127.0.0.1")
            .arg(format!("--port={}", self.upstream_port))
            .args(render_engine_args(&self.engine_args)?);
        if !self.engine_args.contains_key("served-model-name") {
            command.arg(format!("--served-model-name={}", self.artifacts.model));
        }
        command.env("VLLM_USE_MODELSCOPE", "false");
        Ok(command)
    }

    /// Rejects incomplete controller input before serving can create native resources.
    fn validate(&self) -> Result<(), String> {
        if self.version != 2 {
            return Err(format!(
                "unsupported vLLM-Omni launch plan version {}",
                self.version
            ));
        }
        for (name, value) in [
            ("model", &self.artifacts.model),
            ("revision", &self.artifacts.revision),
            ("tokenizer", &self.artifacts.tokenizer),
            ("tokenizerRevision", &self.artifacts.tokenizer_revision),
        ] {
            if value.is_empty() {
                return Err(format!("artifacts.{name} must be nonempty"));
            }
        }
        if self.upstream_port == 0 {
            return Err("upstreamPort must be positive".into());
        }
        if self.lifecycle.startup_seconds == 0 || self.lifecycle.drain_seconds == 0 {
            return Err("lifecycle timeouts must be positive".into());
        }
        render_engine_args(&self.engine_args).map(|_| ())
    }
}

/// Renders controller-normalized options while retaining adapter ownership of its listener and model.
fn render_engine_args(args: &BTreeMap<String, Value>) -> Result<Vec<String>, String> {
    let mut output = Vec::new();
    for (name, value) in args {
        if name.is_empty()
            || name.starts_with('-')
            || matches!(name.as_str(), "host" | "model" | "omni" | "port")
        {
            return Err(format!("engineArgs.{name} is reserved or invalid"));
        }
        append_engine_arg(&mut output, name, value)?;
    }
    Ok(output)
}

fn required_env(name: &str) -> Result<String, String> {
    match std::env::var(name) {
        Ok(value) if !value.is_empty() => Ok(value),
        _ => Err(format!("{name} must be set")),
    }
}
