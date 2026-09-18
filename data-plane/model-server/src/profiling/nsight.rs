// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Nsight Systems 的进程树插桩、原生控制和报告导出。

use std::io;
use std::process::{Command, Output};

use super::Config;

pub(crate) fn session_name(runtime_id: &str) -> String {
    format!("foretoken-{runtime_id}")
}

/// 配置 CUDA/NVTX 插桩；记录由 runtime 后续显式开启。
pub(crate) fn launch(application: Command, session: &str) -> Command {
    let mut command = Command::new("nsys");
    command.args([
        "launch",
        &format!("--session-new={session}"),
        "--trace=cuda,nvtx",
        "--cuda-graph-trace=node",
        "--trace-fork-before-exec=true",
        "--wait=all",
    ]);
    command
        .arg(application.get_program())
        .args(application.get_args());
    for (name, value) in application.get_envs() {
        match value {
            Some(value) => {
                command.env(name, value);
            }
            None => {
                command.env_remove(name);
            }
        }
    }
    command.env("VLLM_WORKER_MULTIPROC_METHOD", "spawn");
    command
}

/// 终止 session 中的真实目标进程组，供引擎生命周期 owner 回收 launcher。
pub(crate) async fn shutdown(session: &str) -> Result<(), String> {
    run(
        tokio::process::Command::new("nsys").args([
            "shutdown",
            &format!("--session={session}"),
            "--kill=sigkill",
        ]),
        "session shutdown",
    )
    .await
    .map(|_| ())
    .map_err(|error| error.to_string())
}

/// 控制一次记录；停止返回时原生报告已导出，模型继续运行。
pub(super) async fn set_recording(config: &Config, start: bool) -> Result<(), String> {
    let mut command = tokio::process::Command::new("nsys");
    command.arg(if start { "start" } else { "stop" });
    command.arg(format!("--session={}", session_name(&config.runtime_id)));
    if start {
        command.args(["--sample=none", "--cpuctxsw=none"]);
        command.arg(format!(
            "--output={}",
            config.staging().join("capture").display()
        ));
    }
    run(&mut command, if start { "start" } else { "stop" })
        .await
        .map(|_| ())
        .map_err(|error| error.to_string())
}

/// 在 supervisor 的导出预算内生成 SQLite 并读取 GPU 活动，返回发布用结果。
pub(super) async fn validate_report(config: &Config) -> io::Result<bool> {
    let report = config.staging().join("capture.nsys-rep");
    let database = report.with_extension("sqlite");
    run(
        tokio::process::Command::new("nsys")
            .args(["export", "--type=sqlite"])
            .arg(format!("--output={}", database.display()))
            .arg(&report),
        "SQLite export",
    )
    .await?;
    let output = run(
        tokio::process::Command::new(&config.python)
            .arg("/opt/foretoken/python/foretoken_nsys.py")
            .arg(report),
        "report validation",
    )
    .await?;
    serde_json::from_slice(&output.stdout).map_err(io::Error::other)
}

// 每次调用只持有一个原生工具进程；所属 future 超时或取消时由 Tokio 终止并回收。
async fn run(command: &mut tokio::process::Command, operation: &str) -> io::Result<Output> {
    let output = command.kill_on_drop(true).output().await?;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "Nsight {operation} failed: {}{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    Ok(output)
}
