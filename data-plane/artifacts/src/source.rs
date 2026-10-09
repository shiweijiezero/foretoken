// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Activate completed source payloads before starting inference or serving requests.

use std::io;
use std::path::PathBuf;

/// Controller-selected bundle directory, shared with the workload projection.
pub const DIRECTORY_ENV: &str = "FORETOKEN_SOURCE_DIRECTORY";
/// Activated directory inherited by engine and preparation subprocesses.
pub const ACTIVE_DIRECTORY_ENV: &str = "FORETOKEN_ACTIVE_SOURCE_DIRECTORY";
/// Engine payload included in a source-built image.
const ENGINE_DIRECTORY_ENV: &str = "FORETOKEN_ENGINE_DIRECTORY";
const ACTIVE_ENGINE_DIRECTORY_ENV: &str = "FORETOKEN_ACTIVE_ENGINE_DIRECTORY";

/// Activates Python paths and engine settings for the executable selected by the workload.
pub fn activate(binary: &str) -> io::Result<()> {
    let source = std::env::var_os(DIRECTORY_ENV).map(PathBuf::from);
    let engine = source
        .as_ref()
        .filter(|directory| directory.join("engine").is_dir())
        .cloned()
        .or_else(|| std::env::var_os(ENGINE_DIRECTORY_ENV).map(PathBuf::from));
    if source.is_none() && engine.is_none() {
        return Ok(());
    }
    if source == std::env::var_os(ACTIVE_DIRECTORY_ENV).map(PathBuf::from)
        && engine == std::env::var_os(ACTIVE_ENGINE_DIRECTORY_ENV).map(PathBuf::from)
    {
        return Ok(());
    }

    let executable = std::env::current_exe()?;
    let mut paths = Vec::new();
    if let Some(directory) = &source {
        #[derive(serde::Deserialize)]
        struct Bundle {
            component: String,
        }
        let bundle: Bundle =
            serde_json::from_reader(std::fs::File::open(directory.join("complete.json"))?)
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        if binary.strip_prefix("foretoken-") != Some(bundle.component.as_str()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "source bundle belongs to another component",
            ));
        }
        paths.push(directory.join("python"));
    }
    if let Some(directory) = &engine
        && directory.join("engine").is_dir()
    {
        paths.push(directory.join("engine"));
    }
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        paths.extend(std::env::split_paths(&existing));
    }
    let python_path = std::env::join_paths(paths)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
    let mut command = std::process::Command::new(executable);
    command
        .args(std::env::args_os().skip(1))
        .env("PYTHONPATH", python_path);
    if let Some(directory) = &source {
        command.env(ACTIVE_DIRECTORY_ENV, directory);
    }
    if let Some(directory) = &engine {
        // MetaX's loader must select the newly compiled plugin rather than mcoplib.
        #[derive(serde::Deserialize)]
        #[serde(deny_unknown_fields)]
        struct EngineEnvironment {
            #[serde(rename = "USE_PRECOMPILED_KERNEL")]
            use_precompiled_kernel: Option<String>,
        }
        let environment: EngineEnvironment = serde_json::from_reader(std::fs::File::open(
            directory.join("engine-environment.json"),
        )?)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        if let Some(value) = environment.use_precompiled_kernel {
            command.env("USE_PRECOMPILED_KERNEL", value);
        }
        command.env(ACTIVE_ENGINE_DIRECTORY_ENV, directory);
    }
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        Err(command.exec())
    }
    #[cfg(not(unix))]
    {
        let _ = command;
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            "development bundles require a Linux runtime",
        ))
    }
}
