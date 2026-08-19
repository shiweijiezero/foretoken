// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Managed SGLang server process lifecycle.
//!
//! Spawns `python3 -m sglang.launch_server` on loopback, waits for its HTTP
//! health endpoint, and shuts the child down on request. The HTTP server and
//! drain orchestration stay in the binary entrypoint.

use std::time::Duration;

use tokio::process::{Child, Command};

use super::launch_plan::SglangLaunchPlan;

/// A spawned SGLang server child.
pub struct SglangProcess {
    child: Child,
}

impl SglangProcess {
    /// Spawns the SGLang server child.
    pub fn spawn(plan: &SglangLaunchPlan) -> Result<Self, std::io::Error> {
        let args = plan.render_args().map_err(std::io::Error::other)?;
        let (program, rest) = args
            .split_first()
            .ok_or_else(|| std::io::Error::other("sglang launch argv is empty"))?;
        let child = Command::new(program).args(rest).spawn()?;
        Ok(Self { child })
    }

    /// Waits for the child to exit, returning its exit status.
    pub async fn wait_for_exit(&mut self) -> std::io::Result<std::process::ExitStatus> {
        self.child.wait().await
    }

    /// Terminates the child, then waits for it to exit.
    pub async fn shutdown(&mut self, grace: Duration) -> std::io::Result<()> {
        if self.child.try_wait()?.is_some() {
            return Ok(());
        }
        self.child.kill().await?;
        let _ = tokio::time::timeout(grace, self.child.wait()).await;
        Ok(())
    }
}
