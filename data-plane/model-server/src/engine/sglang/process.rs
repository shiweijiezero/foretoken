// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Lifecycle wrapper for the managed SGLang server process.

use std::time::Duration;

use tokio::process::{Child, Command};

use super::launch_plan::SglangLaunchPlan;

/// Managed SGLang server child.
pub struct SglangProcess {
    child: Child,
}

impl SglangProcess {
    /// Spawns SGLang from the launch plan.
    pub fn spawn(plan: &SglangLaunchPlan) -> Result<Self, std::io::Error> {
        let args = plan.render_args();
        let (program, rest) = args
            .split_first()
            .ok_or_else(|| std::io::Error::other("sglang launch argv is empty"))?;
        let child = Command::new(program).args(rest).spawn()?;
        Ok(Self { child })
    }

    /// Waits for the child to exit.
    pub async fn wait_for_exit(&mut self) -> std::io::Result<std::process::ExitStatus> {
        self.child.wait().await
    }

    /// Terminates the child and waits up to the grace period.
    pub async fn shutdown(&mut self, grace: Duration) -> std::io::Result<()> {
        if self.child.try_wait()?.is_some() {
            return Ok(());
        }
        self.child.kill().await?;
        let _ = tokio::time::timeout(grace, self.child.wait()).await;
        Ok(())
    }
}
