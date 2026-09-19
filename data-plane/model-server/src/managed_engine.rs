// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine process ownership, including teardown of an optional Nsight session.

use std::process::{Command, ExitStatus};
use std::time::Duration;

use tokio::time::{Instant, timeout_at};
use vllm_managed_engine::ManagedEngineHandle;

use crate::profiling::{Config, Engine, nsight};

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
