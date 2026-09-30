// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Activate a completed development bundle before starting inference or serving requests.

use std::io;
use std::path::PathBuf;

/// Controller-selected bundle directory, shared with the workload projection.
pub const DIRECTORY_ENV: &str = "FORETOKEN_SOURCE_DIRECTORY";
/// Activated directory inherited by engine and preparation subprocesses.
pub const ACTIVE_DIRECTORY_ENV: &str = "FORETOKEN_ACTIVE_SOURCE_DIRECTORY";

/// Runs the selected source executable with its Python adapters, or leaves release startup unchanged.
/// Workload controllers supply the directory only for source-installed platforms.
pub fn activate(binary: &str) -> io::Result<()> {
    let Some(directory) = std::env::var_os(DIRECTORY_ENV) else {
        return Ok(());
    };
    if std::env::var_os(ACTIVE_DIRECTORY_ENV).as_ref() == Some(&directory) {
        return Ok(());
    }
    let directory = PathBuf::from(directory);
    #[derive(serde::Deserialize)]
    struct Bundle {
        revision: String,
        component: String,
        executable: Option<String>,
    }
    let bundle: Bundle =
        serde_json::from_reader(std::fs::File::open(directory.join("complete.json"))?)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    if directory.file_name().and_then(|name| name.to_str()) != Some(bundle.revision.as_str())
        || binary.strip_prefix("foretoken-") != Some(bundle.component.as_str())
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "source bundle belongs to another runtime revision",
        ));
    }
    let executable = match bundle.executable {
        Some(name) if name == binary => directory.join("bin").join(name),
        Some(_) => {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "source executable does not match the runtime",
            ));
        }
        None => std::env::current_exe()?,
    };
    let mut paths = vec![directory.join("python")];
    if let Some(existing) = std::env::var_os("PYTHONPATH") {
        paths.extend(std::env::split_paths(&existing));
    }
    let python_path = std::env::join_paths(paths)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error))?;
    let mut command = std::process::Command::new(executable);
    command
        .args(std::env::args_os().skip(1))
        .env("PYTHONPATH", python_path)
        .env(ACTIVE_DIRECTORY_ENV, &directory);
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
