// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Nsight Systems process-tree instrumentation and synchronous report export.

use std::io;
use std::process::Command;

use super::Config;

/// Prepares CUDA/NVTX tracing without collecting until this runtime starts a capture.
pub(super) fn launch(application: Command, runtime_id: &str) -> Command {
    let mut command = Command::new("nsys");
    command.args([
        "launch",
        &format!("--session-new=foretoken-{runtime_id}"),
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

/// Starts or stops one named collection; stop returns after the native report is generated.
pub(super) async fn set_recording(config: &Config, start: bool) -> Result<(), String> {
    let mut command = tokio::process::Command::new("nsys");
    command.arg(if start { "start" } else { "stop" });
    command.arg(format!("--session=foretoken-{}", config.runtime_id));
    if start {
        command.args(["--sample=none", "--cpuctxsw=none"]);
        command.arg(format!(
            "--output={}",
            config.staging().join("capture").display()
        ));
    }
    let output = command
        .kill_on_drop(true)
        .output()
        .await
        .map_err(|error| error.to_string())?;
    if !output.status.success() {
        return Err(format!(
            "Nsight {} failed: {}{}",
            if start { "start" } else { "stop" },
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    Ok(())
}

/// Validates the native report with NVIDIA's exporter and inspects kernel activity before publication.
pub(super) fn validate_report(config: &Config) -> io::Result<bool> {
    let output = Command::new(&config.python)
        .arg("/opt/foretoken/python/foretoken_nsys.py")
        .arg(config.staging().join("capture.nsys-rep"))
        .output()?;
    if !output.status.success() {
        return Err(io::Error::other(format!(
            "Nsight report validation failed: {}",
            String::from_utf8_lossy(&output.stderr)
        )));
    }
    serde_json::from_slice(&output.stdout).map_err(io::Error::other)
}
