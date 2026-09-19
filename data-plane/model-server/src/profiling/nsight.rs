// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Nsight Systems process-tree instrumentation, native control, and report export.

use std::io;
use std::process::{Command, Output};

use super::Config;

pub(crate) fn session_name(runtime_id: &str) -> String {
    format!("foretoken-{runtime_id}")
}

/// Prepares CUDA/NVTX instrumentation without recording until the runtime requests a capture.
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

/// Terminates the session's target process group before the engine owner reaps its launcher.
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

/// Controls one recording; stop returns after native report export while the model keeps serving.
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

/// Exports SQLite and inspects GPU activity under the supervisor's publication deadline.
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
            .arg(database),
        "report validation",
    )
    .await?;
    serde_json::from_slice(&output.stdout).map_err(io::Error::other)
}

// Each future owns one native tool process; Tokio kills it on cancellation and reaps it.
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
