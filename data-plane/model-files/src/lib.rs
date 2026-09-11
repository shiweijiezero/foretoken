// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolve local model and tokenizer directories without changing remote model identifiers.

use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};

/// Resolve a directory for frontend or model-server, relative to an optional cache root.
///
/// A missing relative directory leaves the identifier available for Hub resolution.
/// Absolute directories keep their Pod-local meaning. Relative links must stay inside
/// the cache; individual files are not replaced by their parent directory. The inference
/// engine owns validation of the files within a resolved model or tokenizer directory.
pub fn resolve_directory(root: Option<&Path>, identifier: &str) -> io::Result<Option<PathBuf>> {
    let path = Path::new(identifier);
    if path.is_absolute() {
        return if path.is_dir() {
            Ok(Some(path.to_path_buf()))
        } else {
            Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("local artifact {identifier:?} must be a model or tokenizer directory"),
            ))
        };
    }
    let Some(root) = root else {
        if path.is_file() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("local artifact {identifier:?} must be a model or tokenizer directory"),
            ));
        }
        return Ok(path.is_dir().then(|| path.to_path_buf()));
    };
    if path
        .components()
        .any(|component| matches!(component, Component::ParentDir))
    {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("artifact {identifier:?} must stay below the cache directory"),
        ));
    }
    let directory = match fs::canonicalize(root.join(path)) {
        Ok(directory) => directory,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error),
    };
    if !directory.starts_with(fs::canonicalize(root)?) || !directory.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("artifact {identifier:?} must be a directory within the cache"),
        ));
    }
    Ok(Some(directory))
}
