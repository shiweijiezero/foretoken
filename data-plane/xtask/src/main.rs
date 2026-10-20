// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::collections::BTreeSet;
use std::env;
use std::ffi::OsStr;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus};

use serde::{Deserialize, Serialize};

const METADATA_SECTION: &str = "[workspace.metadata.foretoken]";
const VLLM_SOURCE_KEY: &str = "vllm_source";
const VLLM_PATCH_CATALOG_KEY: &str = "vllm_patch_catalog";

#[derive(Debug)]
struct WorkspaceMetadata {
    data_plane_root: PathBuf,
    vllm_source: PathBuf,
    vllm_patches: Vec<PathBuf>,
}

#[derive(Debug)]
struct MetadataValues {
    vllm_source: String,
    vllm_patch_catalog: String,
}

#[derive(Deserialize)]
struct CatalogPatch {
    path: PathBuf,
    #[serde(default)]
    rust: bool,
}

// Source preparation reads only the catalog fields owned by the pinned Rust checkout.
#[derive(Deserialize)]
struct PatchCatalog {
    patches: Vec<CatalogPatch>,
}

#[derive(Clone, Deserialize, Serialize, PartialEq, Eq)]
struct PreparedPatch {
    path: PathBuf,
    contents: String,
}

/// Run repository build tooling and report task failures to the invoking command.
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
    let catalog_path = data_plane_root.join(values.vllm_patch_catalog);
    let patch_root = catalog_path
        .parent()
        .ok_or_else(|| "vLLM patch catalog has no parent directory".to_owned())?;
    let contents = fs::read_to_string(&catalog_path)
        .map_err(|error| format!("read {}: {error}", catalog_path.display()))?;
    let catalog: PatchCatalog = serde_json::from_str(&contents)
        .map_err(|error| format!("parse {}: {error}", catalog_path.display()))?;
    let vllm_patches = catalog
        .patches
        .into_iter()
        .filter(|patch| patch.rust)
        .map(|patch| patch_root.join(patch.path))
        .collect();
    Ok(WorkspaceMetadata {
        data_plane_root,
        vllm_source,
        vllm_patches,
    })
}

/// Read the two repository paths from Cargo workspace metadata.
fn metadata_values(manifest: &str) -> Result<MetadataValues, String> {
    let mut in_section = false;
    let mut vllm_source = None;
    let mut vllm_patch_catalog = None;

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
        if let Some(value) = line.strip_prefix(&format!("{VLLM_PATCH_CATALOG_KEY} =")) {
            vllm_patch_catalog = Some(parse_string(value)?);
        }
    }

    Ok(MetadataValues {
        vllm_source: vllm_source.ok_or_else(|| format!("missing {VLLM_SOURCE_KEY} metadata"))?,
        vllm_patch_catalog: vllm_patch_catalog
            .ok_or_else(|| format!("missing {VLLM_PATCH_CATALOG_KEY} metadata"))?,
    })
}

fn parse_string(value: &str) -> Result<String, String> {
    let value = value.trim().trim_end_matches(',');
    let value = value
        .strip_prefix('"')
        .and_then(|value| value.strip_suffix('"'))
        .ok_or_else(|| format!("expected quoted metadata value, got {value:?}"))?;
    Ok(value.to_owned())
}

/// Prepare the complete selected stack privately before updating Cargo's source tree.
/// The retained stack travels with captured Rust inputs so removals and interrupted writes recover.
fn prepare_vllm(metadata: &WorkspaceMetadata) -> Result<(), String> {
    let staging = tempfile::Builder::new()
        .prefix("foretoken-vllm-")
        .tempdir()
        .map_err(|error| format!("create vLLM preparation directory: {error}"))?;
    match prepare_patch_tree(metadata, staging.path()) {
        Ok(()) => Ok(()),
        Err(error) => {
            let retained = staging.keep();
            Err(format!(
                "{error}; preparation retained at {}",
                retained.display()
            ))
        }
    }
}

/// Reconstruct managed changes using Git's hunk matching, preserving unrelated source edits.
fn prepare_patch_tree(metadata: &WorkspaceMetadata, staging: &Path) -> Result<(), String> {
    let state_path = metadata.vllm_source.join(".foretoken-patches.json");
    let saved: Option<Vec<PreparedPatch>> = match fs::read(&state_path) {
        Ok(contents) => Some(
            serde_json::from_slice(&contents)
                .map_err(|error| format!("parse {}: {error}", state_path.display()))?,
        ),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(error) => return Err(format!("read {}: {error}", state_path.display())),
    };
    let known_count = saved.as_ref().map(Vec::len);
    let mut previous = saved.unwrap_or_default();
    let current = metadata
        .vllm_patches
        .iter()
        .map(|path| {
            Ok(PreparedPatch {
                path: path
                    .strip_prefix(&metadata.data_plane_root)
                    .map_err(|error| {
                        format!("locate repository patch {}: {error}", path.display())
                    })?
                    .to_path_buf(),
                contents: fs::read_to_string(path)
                    .map_err(|error| format!("read {}: {error}", path.display()))?,
            })
        })
        .collect::<Result<Vec<_>, String>>()?;
    // The saved record owns possible prior writes. Only a checkout without that
    // record needs the current catalog to recognize pre-existing managed changes.
    if !previous.ends_with(&current) {
        previous.extend(current.iter().cloned());
    }
    let normalize_count = known_count.unwrap_or(previous.len());
    let tree = staging.join("tree");
    fs::create_dir(&tree).map_err(|error| format!("create patch tree: {error}"))?;
    let mut targets = BTreeSet::new();
    let mut patch_paths = Vec::new();
    for (index, patch) in previous.iter().enumerate() {
        let path = staging.join(format!("{index}.patch"));
        fs::write(&path, &patch.contents).map_err(|error| format!("stage patch: {error}"))?;
        let before = patch_targets(&tree, &path, true)?;
        let after = patch_targets(&tree, &path, false)?;
        targets.extend(before.iter().chain(&after).cloned());
        // Reverse application also reverses the file-record order.
        let renames = before
            .into_iter()
            .rev()
            .zip(after)
            .filter(|(before, after)| before != after)
            .collect::<Vec<_>>();
        patch_paths.push((path, renames));
    }
    for name in &targets {
        let source = metadata.vllm_source.join(name);
        let target = tree.join(name);
        fs::create_dir_all(target.parent().ok_or("patch target has no parent")?)
            .map_err(|error| format!("create patch target parent: {error}"))?;
        match fs::copy(&source, &target) {
            Ok(_) => {}
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => return Err(format!("stage {}: {error}", source.display())),
        }
    }
    // Captured inputs can contain only the Rust half of an earlier stack. Reverse
    // matching hunks privately, then require every current patch to apply in full.
    let log = fs::File::create(staging.join("bootstrap.log"))
        .map_err(|error| format!("create patch diagnostics: {error}"))?;
    for (index, (patch, renames)) in patch_paths[..normalize_count].iter().rev().enumerate() {
        let mut duplicates = Vec::new();
        let mut reverse = Command::new("git");
        reverse
            .current_dir(&tree)
            .args(["apply", "--reverse", "--reject", "--whitespace=nowarn"]);
        for (number, (before, after)) in renames.iter().enumerate() {
            let original = tree.join(before);
            let backup = if original.is_file() && tree.join(after).is_file() {
                // Destinations are published before sources are removed.
                let backup = staging.join(format!("rename-{index}-{number}"));
                fs::rename(&original, &backup).map_err(|error| error.to_string())?;
                Some(backup)
            } else {
                None
            };
            // Git can move a file even when --reject rejects its content hunk.
            // Reverse a rename only when that complete file variant matches.
            let checked = Command::new("git")
                .current_dir(&tree)
                .args(["apply", "--reverse", "--check"])
                .arg(format!("--include={}", before.display()))
                .arg(patch)
                .stdout(log.try_clone().map_err(|error| error.to_string())?)
                .stderr(log.try_clone().map_err(|error| error.to_string())?)
                .status()
                .map_err(|error| error.to_string())?;
            match checked.code() {
                Some(0) => {
                    if let Some(backup) = backup {
                        duplicates.push((original, backup));
                    }
                }
                Some(1) => {
                    if let Some(backup) = backup {
                        fs::rename(backup, original).map_err(|error| error.to_string())?;
                    }
                    reverse.arg(format!("--exclude={}", before.display()));
                }
                _ => return Err(format!("inspect prepared rename exited with {checked}")),
            }
        }
        let status = reverse
            .arg(patch)
            .stdout(log.try_clone().map_err(|error| error.to_string())?)
            .stderr(log.try_clone().map_err(|error| error.to_string())?)
            .status()
            .map_err(|error| format!("normalize prepared source: {error}"))?;
        if !matches!(status.code(), Some(0 | 1)) {
            return Err(format!("normalize prepared source exited with {status}"));
        }
        for (original, backup) in duplicates {
            let restored = fs::read(&original)
                .map_err(|error| format!("restore rename {}: {error}", original.display()))?;
            if restored != fs::read(&backup).map_err(|error| error.to_string())? {
                return Err(format!(
                    "rename conflicts with retained source {}",
                    original.display()
                ));
            }
            fs::set_permissions(
                &original,
                fs::metadata(&backup)
                    .map_err(|error| error.to_string())?
                    .permissions(),
            )
            .map_err(|error| error.to_string())?;
        }
    }
    for path in &metadata.vllm_patches {
        run_command(&tree, "git", [OsStr::new("apply"), path.as_os_str()])?;
    }

    // Record every possible managed layer before publishing files. A later call
    // can undo a partial write even if the catalog changed after interruption.
    write_patch_state(&state_path, &previous)?;
    for present in [true, false] {
        for name in targets
            .iter()
            .filter(|name| tree.join(name).is_file() == present)
        {
            publish_prepared_file(&tree.join(name), &metadata.vllm_source.join(name))
                .map_err(|error| format!("publish prepared {}: {error}", name.display()))?;
        }
    }
    write_patch_state(&state_path, &current)
}

/// Ask Git for each side's paths, including additions, deletions, and renames.
fn patch_targets(directory: &Path, patch: &Path, reverse: bool) -> Result<Vec<PathBuf>, String> {
    let mut command = Command::new("git");
    command
        .current_dir(directory)
        .args(["apply", "--numstat", "-z"]);
    if reverse {
        command.arg("--reverse");
    }
    let output = command
        .arg(patch)
        .output()
        .map_err(|error| format!("read patch targets: {error}"))?;
    if !output.status.success() {
        return Err(format!(
            "read patch targets: {}",
            String::from_utf8_lossy(&output.stderr)
        ));
    }
    String::from_utf8(output.stdout)
        .map_err(|error| format!("decode patch paths: {error}"))?
        .split('\0')
        .filter(|record| !record.is_empty())
        .map(|record| {
            record
                .splitn(3, '\t')
                .nth(2)
                .map(PathBuf::from)
                .ok_or_else(|| "git apply returned an invalid file statistic".to_owned())
        })
        .collect()
}

/// Atomically publish preparation ownership without invalidating unchanged input timestamps.
fn write_patch_state(path: &Path, patches: &[PreparedPatch]) -> Result<(), String> {
    let mut contents = serde_json::to_vec_pretty(patches).map_err(|error| error.to_string())?;
    contents.push(b'\n');
    match fs::read(path) {
        Ok(previous) if previous == contents => return Ok(()),
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(format!("read preparation state: {error}")),
    }
    let mut temporary =
        tempfile::NamedTempFile::new_in(path.parent().ok_or("state has no parent")?)
            .map_err(|error| error.to_string())?;
    temporary
        .write_all(&contents)
        .map_err(|error| error.to_string())?;
    temporary
        .persist(path)
        .map_err(|error| format!("publish preparation state: {}", error.error))?;
    Ok(())
}

/// Publish a prepared file or deletion while keeping unchanged Cargo inputs at their existing mtime.
fn publish_prepared_file(source: &Path, target: &Path) -> std::io::Result<()> {
    let contents = match fs::read(source) {
        Ok(contents) => contents,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return match fs::remove_file(target) {
                Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
                result => result,
            };
        }
        Err(error) => return Err(error),
    };
    let permissions = fs::metadata(source)?.permissions();
    match fs::read(target) {
        Ok(previous) if previous == contents => return fs::set_permissions(target, permissions),
        Ok(_) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let parent = target
        .parent()
        .ok_or_else(|| std::io::Error::other("prepared file has no parent"))?;
    fs::create_dir_all(parent)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    temporary.write_all(&contents)?;
    temporary.as_file().set_permissions(permissions)?;
    temporary.persist(target).map_err(|error| error.error)?;
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
