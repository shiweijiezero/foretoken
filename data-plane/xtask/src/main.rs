// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Prepares Rust patches and runs Cargo with the repository's compiler dependencies.

#[path = "../../patches/vllm/source.rs"]
mod vllm_source;

use std::env;
use std::ffi::{OsStr, OsString};
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Deserialize;

struct WorkspaceMetadata {
    data_plane_root: PathBuf,
    vllm_source: PathBuf,
    vllm_patch_catalog: PathBuf,
}

#[derive(Deserialize)]
struct WorkspaceManifest {
    workspace: WorkspaceSection,
}

#[derive(Deserialize)]
struct WorkspaceSection {
    metadata: WorkspaceMetadataSection,
}

#[derive(Deserialize)]
struct WorkspaceMetadataSection {
    foretoken: MetadataValues,
}

#[derive(Deserialize)]
struct MetadataValues {
    vllm_source: PathBuf,
    vllm_patch_catalog: PathBuf,
}

/// Runs repository build tooling and reports task failures to the invoking command.
fn main() {
    if let Err(error) = run() {
        eprintln!("foretoken-xtask: {error}");
        std::process::exit(1);
    }
}

/// Dispatches the stages sequenced by Make and the shared source-build entrypoint.
fn run() -> Result<(), String> {
    let mut args = env::args_os().skip(1);
    let task = args.next().ok_or_else(usage)?;
    let metadata = workspace_metadata()?;
    match task.to_str() {
        Some("prepare-vllm") => vllm_source::prepare(
            &metadata.data_plane_root,
            &metadata.vllm_source,
            &metadata.vllm_patch_catalog,
        ),
        Some("check") => {
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
        Some("build") => {
            let mut args = args.collect::<Vec<_>>();
            if args.first().is_some_and(|arg| arg == "--") {
                args.remove(0);
            }
            if args.is_empty() {
                args.push("--workspace".into());
            }
            let mut command = vec![OsString::from("build"), "--locked".into()];
            command.extend(args);
            cargo(&metadata.data_plane_root, command)
        }
        _ => Err(usage()),
    }
}

/// Reads source/catalog paths from Cargo's typed workspace metadata.
fn workspace_metadata() -> Result<WorkspaceMetadata, String> {
    // Cargo may reuse this binary across source directories sharing one target cache.
    let data_plane_root = PathBuf::from(
        env::var_os("CARGO_MANIFEST_DIR")
            .ok_or("Cargo did not provide the xtask manifest directory")?,
    )
    .parent()
    .ok_or("xtask manifest has no workspace parent")?
    .to_path_buf();
    let manifest_path = data_plane_root.join("Cargo.toml");
    let manifest = fs::read_to_string(&manifest_path)
        .map_err(|error| format!("read {}: {error}", manifest_path.display()))?;
    let manifest: WorkspaceManifest = toml::from_str(&manifest)
        .map_err(|error| format!("parse {}: {error}", manifest_path.display()))?;
    let values = manifest.workspace.metadata.foretoken;
    Ok(WorkspaceMetadata {
        vllm_source: data_plane_root.join(values.vllm_source),
        vllm_patch_catalog: data_plane_root.join(values.vllm_patch_catalog),
        data_plane_root,
    })
}

/// Runs Cargo with the registry-provided host protoc, including its standard protobuf imports.
fn cargo<I, S>(working_dir: &Path, args: I) -> Result<(), String>
where
    I: IntoIterator<Item = S>,
    S: AsRef<OsStr>,
{
    let args = args
        .into_iter()
        .map(|arg| arg.as_ref().to_owned())
        .collect::<Vec<_>>();
    let protoc = protoc_bin_vendored::protoc_bin_path().map_err(|error| error.to_string())?;
    let includes = protoc_bin_vendored::include_path().map_err(|error| error.to_string())?;
    let status = Command::new("cargo")
        .current_dir(working_dir)
        .env("PROTOC", protoc)
        .env("PROTOC_INCLUDE", includes)
        .args(&args)
        .status()
        .map_err(|error| format!("run cargo: {error}"))?;
    if status.success() {
        return Ok(());
    }
    let args = args
        .iter()
        .map(|arg| arg.to_string_lossy())
        .collect::<Vec<_>>()
        .join(" ");
    Err(format!("cargo {args} exited with {status}"))
}

fn usage() -> String {
    "usage: cargo xtask <prepare-vllm|check|build [-- <cargo build arguments>]>".to_owned()
}
