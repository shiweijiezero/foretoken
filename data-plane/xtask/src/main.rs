// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::env;
use std::ffi::OsStr;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus, Stdio};

const METADATA_SECTION: &str = "[workspace.metadata.foretoken]";
const VLLM_SOURCE_KEY: &str = "vllm_source";
const VLLM_PATCH_SERIES_KEY: &str = "vllm_patch_series";

#[derive(Debug)]
struct WorkspaceMetadata {
    data_plane_root: PathBuf,
    vllm_source: PathBuf,
    vllm_patches: Vec<PathBuf>,
}

fn main() {
    if let Err(error) = run() {
        eprintln!("foretoken-xtask: {error}");
        std::process::exit(1);
    }
}

/// Dispatch a repository build task from the Cargo workspace root.
fn run() -> Result<(), String> {
    let task = env::args().nth(1).ok_or_else(usage)?;
    let metadata = workspace_metadata()?;

    match task.as_str() {
        "prepare-vllm" => prepare_vllm(&metadata),
        "check" => {
            prepare_vllm(&metadata)?;
            cargo(&metadata.data_plane_root, ["fmt", "--all", "--", "--check"])?;
            cargo(
                &metadata.data_plane_root,
                [
                    "clippy",
                    "--workspace",
                    "--all-targets",
                    "--locked",
                    "--",
                    "-D",
                    "warnings",
                ],
            )
        }
        "build" => {
            prepare_vllm(&metadata)?;
            cargo(
                &metadata.data_plane_root,
                ["build", "--workspace", "--locked"],
            )
        }
        _ => Err(usage()),
    }
}

/// Read the vLLM source and patch paths owned by the data-plane workspace.
fn workspace_metadata() -> Result<WorkspaceMetadata, String> {
    let data_plane_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .ok_or_else(|| "xtask manifest has no workspace parent".to_owned())?
        .to_path_buf();
    let manifest_path = data_plane_root.join("Cargo.toml");
    let manifest = fs::read_to_string(&manifest_path)
        .map_err(|error| format!("read {}: {error}", manifest_path.display()))?;
    let values = metadata_values(&manifest)?;
    let vllm_source = data_plane_root.join(values.vllm_source);
    let series_path = data_plane_root.join(values.vllm_patch_series);
    let patch_root = series_path
        .parent()
        .ok_or_else(|| "vLLM patch series has no parent directory".to_owned())?;
    let series = fs::read_to_string(&series_path)
        .map_err(|error| format!("read {}: {error}", series_path.display()))?;
    let vllm_patches = series
        .lines()
        .map(|line| line.split('#').next().unwrap_or_default().trim())
        .filter(|line| !line.is_empty())
        .map(|path| patch_root.join(path))
        .collect();
    Ok(WorkspaceMetadata {
        data_plane_root,
        vllm_source,
        vllm_patches,
    })
}

/// Parse the small, local metadata table without adding a manifest parser dependency.
fn metadata_values(manifest: &str) -> Result<MetadataValues, String> {
    let mut in_section = false;
    let mut vllm_source = None;
    let mut vllm_patch_series = None;

    for line in manifest.lines() {
        let line = line.split('#').next().unwrap_or_default().trim();
        if line.starts_with('[') {
            in_section = line == METADATA_SECTION;
            continue;
        }
        if !in_section || line.is_empty() {
            continue;
        }
        if let Some(value) = line.strip_prefix(&format!("{VLLM_SOURCE_KEY} =")) {
            vllm_source = Some(parse_string(value)?);
            continue;
        }
        if let Some(value) = line.strip_prefix(&format!("{VLLM_PATCH_SERIES_KEY} =")) {
            vllm_patch_series = Some(parse_string(value)?);
        }
    }

    Ok(MetadataValues {
        vllm_source: vllm_source.ok_or_else(|| format!("missing {VLLM_SOURCE_KEY} metadata"))?,
        vllm_patch_series: vllm_patch_series
            .ok_or_else(|| format!("missing {VLLM_PATCH_SERIES_KEY} metadata"))?,
    })
}

#[derive(Debug)]
struct MetadataValues {
    vllm_source: String,
    vllm_patch_series: String,
}

fn parse_string(value: &str) -> Result<String, String> {
    let value = value.trim().trim_end_matches(',');
    let value = value
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
        .ok_or_else(|| format!("expected quoted metadata value, got {value:?}"))?;
    Ok(value.to_owned())
}

/// Initialize the pinned vLLM submodule and apply each repository-owned patch once.
fn prepare_vllm(metadata: &WorkspaceMetadata) -> Result<(), String> {
    let rust_manifest = metadata.vllm_source.join("rust/Cargo.toml");
    if !rust_manifest.is_file() {
        run_command(
            &metadata.data_plane_root,
            "git",
            [
                "submodule",
                "update",
                "--init",
                "--",
                "data-plane/third_party/vllm",
            ],
        )?;
    }

    for patch in &metadata.vllm_patches {
        let patch_path = patch.to_string_lossy();
        let reverse_check = Command::new("git")
            .current_dir(&metadata.vllm_source)
            .args(["apply", "--reverse", "--check", patch_path.as_ref()])
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .status()
            .map_err(|error| format!("run git apply check: {error}"))?;
        if reverse_check.success() {
            continue;
        }
        run_command(&metadata.vllm_source, "git", ["apply", patch_path.as_ref()])?;
    }
    Ok(())
}

fn cargo<'a, I>(working_dir: &Path, args: I) -> Result<(), String>
where
    I: IntoIterator<Item = &'a str>,
{
    run_command(working_dir, "cargo", args)
}

fn run_command<I, S>(working_dir: &Path, program: &str, args: I) -> Result<(), String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    let args: Vec<_> = args
        .into_iter()
        .map(|arg| arg.as_ref().to_owned())
        .collect();
    let status = Command::new(program)
        .current_dir(working_dir)
        .args(&args)
        .status()
        .map_err(|error| format!("run {program}: {error}"))?;
    if status.success() {
        return Ok(());
    }
    Err(command_failure(program, &args, status))
}

fn command_failure(program: &str, args: &[std::ffi::OsString], status: ExitStatus) -> String {
    let args = args
        .iter()
        .map(|arg| arg.to_string_lossy())
        .collect::<Vec<_>>()
        .join(" ");
    format!("{program} {args} exited with {status}")
}

fn usage() -> String {
    "usage: cargo xtask <prepare-vllm|check|build>".to_owned()
}
