// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! 管理引擎进程及可选 Nsight session 的启动、观察和完整退出。

use std::process::{Command, ExitStatus};
use std::time::Duration;

use tokio::time::{Instant, timeout_at};
use vllm_managed_engine::ManagedEngineHandle;

use crate::profiling::{Config, Engine, nsight};

/// model-server 持有的引擎生命周期；Nsight 目标进程不属于 launcher 的进程组。
pub struct ManagedEngine {
    handle: ManagedEngineHandle,
    nsight_session: Option<String>,
}

impl ManagedEngine {
    /// 启动已配置环境的引擎命令，并接管其进程及诊断 session。
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

    /// 向主循环报告 launcher 的退出；退出清理仍由 shutdown 完成。
    pub async fn wait_for_exit(&self) -> ExitStatus {
        self.handle.wait_for_exit().await
    }

    /// 排空请求后终止真实引擎，再回收 launcher；失败时不确认引擎已停止。
    pub async fn shutdown(&self, timeout: Duration) -> Result<(), String> {
        let deadline = Instant::now() + timeout;
        if let Some(session) = &self.nsight_session
            && !self
                .handle
                .try_wait()
                .await
                .is_some_and(|status| status.success())
        {
            // Nsight 的 SIGTERM shutdown 会删除 session，即使目标未退出；使用原生
            // SIGKILL shutdown 保留退出 ownership，不依赖外层进程组代为清理。
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
