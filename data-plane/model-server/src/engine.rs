// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns native engine processes and the launch primitives shared by model-server adapters.

use std::process::{Command, ExitStatus};
use std::time::Duration;

use serde::Deserialize;
use tokio::time::{Instant, timeout_at};
use vllm_managed_engine::ManagedEngineHandle;

use crate::profiling::{Config, Engine, nsight};

const VLLM_PYTHON_ENV: &str = "FORETOKEN_VLLM_PYTHON";
const DEFAULT_VLLM_PYTHON: &str = "python3";

/// Python adapters bundled with the model-server image for its managed engine.
pub const PYTHON_MODULE_PATH: &str = "/opt/foretoken/python";

/// Controller-owned startup and drain budgets shared by native-engine launch plans.
#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Lifecycle {
    #[serde(rename = "startupSeconds")]
    pub startup_seconds: u64,
    #[serde(rename = "drainSeconds")]
    pub drain_seconds: u64,
}

/// Model-server's process owner; Nsight targets do not share the launcher's process group.
pub struct ManagedEngine {
    handle: ManagedEngineHandle,
    nsight_session: Option<String>,
}

impl ManagedEngine {
    /// Starts the configured engine command and retains its process and diagnostic session.
    pub async fn spawn(command: Command, profiling: Option<&Config>) -> Result<Self, String> {
        let nsight_session = profiling
            .filter(|config| config.engine == Engine::Nsight)
            .map(|config| nsight::session_name(&config.runtime_id));
        let command = match &nsight_session {
            Some(session) => nsight::launch(command, session),
            None => command,
        };
        let handle = ManagedEngineHandle::spawn_command(command)
            .await
            .map_err(|error| error.to_string())?;
        Ok(Self {
            handle,
            nsight_session,
        })
    }

    /// Reports launcher exit to the main loop; shutdown still owns target cleanup.
    pub async fn wait_for_exit(&self) -> ExitStatus {
        self.handle.wait_for_exit().await
    }

    /// Polls the managed process during adapter startup without taking process ownership.
    pub async fn try_wait(&self) -> Option<ExitStatus> {
        self.handle.try_wait().await
    }

    /// Stops the actual engine after request drain, then reaps the launcher or reports failure.
    pub async fn shutdown(&self, timeout: Duration) -> Result<(), String> {
        let deadline = Instant::now() + timeout;
        if let Some(session) = &self.nsight_session
            && !self
                .handle
                .try_wait()
                .await
                .is_some_and(|status| status.success())
        {
            // SIGTERM shutdown discards the session even when its target ignores the signal.
            // Native SIGKILL shutdown reaches targets outside the launcher's process group.
            timeout_at(deadline, nsight::shutdown(session))
                .await
                .map_err(|_| "Nsight session shutdown timed out".to_string())??;
        }
        self.handle
            .shutdown(deadline.saturating_duration_since(Instant::now()))
            .await
            .map_err(|error| error.to_string())
    }
}

/// Selects the image's interpreter for engine, provider, report, and cache-worker subprocesses.
pub fn python_executable() -> String {
    std::env::var(VLLM_PYTHON_ENV)
        .ok()
        .filter(|python| !python.is_empty())
        .unwrap_or_else(|| DEFAULT_VLLM_PYTHON.into())
}

/// Appends one controller-normalized engine option to a shell-free command line.
pub fn append_engine_arg(
    args: &mut Vec<String>,
    name: &str,
    value: &serde_json::Value,
) -> Result<(), String> {
    match value {
        serde_json::Value::Null => {}
        serde_json::Value::Bool(enabled) => args.push(if *enabled {
            format!("--{name}")
        } else {
            format!("--no-{name}")
        }),
        serde_json::Value::String(value) => args.push(format!("--{name}={value}")),
        serde_json::Value::Array(values) => {
            args.push(format!("--{name}"));
            for value in values {
                let value = match value {
                    serde_json::Value::String(value) => value.clone(),
                    value => value.to_string(),
                };
                // A list item must not become a separate CLI option.
                if value.starts_with('-') && value.parse::<f64>().is_err() {
                    return Err(format!(
                        "engineArgs.{name} contains an option-like list value"
                    ));
                }
                args.push(value);
            }
        }
        value => args.push(format!("--{name}={value}")),
    }
    Ok(())
}
