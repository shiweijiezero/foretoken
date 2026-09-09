// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Owns one bounded engine profile and its files until the operator collects them.

use std::collections::BTreeSet;
use std::io;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use axum::Json;
use axum::extract::{Path, State};
use axum::http::StatusCode;
use serde::{Deserialize, Serialize};
use tokio::sync::Mutex;
use uuid::Uuid;

use crate::backend::VllmBackend;

/// Resolves the platform-selected profiler directory for both engine launch and file collection.
pub fn output_directory() -> io::Result<Option<PathBuf>> {
    match std::env::var_os("FORETOKEN_PROFILE_DIR") {
        Some(value) if !value.is_empty() => {
            let path = PathBuf::from(value);
            if !path.is_absolute() {
                return Err(io::Error::new(
                    io::ErrorKind::InvalidInput,
                    "FORETOKEN_PROFILE_DIR must be absolute",
                ));
            }
            Ok(Some(path))
        }
        _ => Ok(None),
    }
}

struct Session {
    id: String,
    active: bool,
    expired: bool,
    existing: BTreeSet<PathBuf>,
}

/// Serializes profile start, stop, and collection without blocking inference admission.
pub struct Profiler {
    backend: Arc<VllmBackend>,
    directory: PathBuf,
    session: Mutex<Option<Session>>,
}

impl Profiler {
    /// Creates the opt-in profiler used by internal HTTP routes; the server owns its lifetime.
    pub fn from_env(backend: Arc<VllmBackend>) -> io::Result<Option<Arc<Self>>> {
        let Some(directory) = output_directory()? else {
            return Ok(None);
        };
        std::fs::create_dir_all(&directory)?;
        Ok(Some(Arc::new(Self {
            backend,
            directory,
            session: Mutex::new(None),
        })))
    }

    async fn stop_locked(&self, session: &mut Session) -> Result<(), StatusCode> {
        if session.active {
            self.backend
                .profile(false, &session.id)
                .await
                .map_err(|_| StatusCode::BAD_GATEWAY)?;
            session.active = false;
        }
        // vLLM may retain its first Torch worker name across captures. File-set differences,
        // rather than the caller's prefix, identify this session's newly flushed artifacts.
        for entry in
            std::fs::read_dir(&self.directory).map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
        {
            let entry = entry.map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
            if entry
                .file_type()
                .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?
                .is_file()
                && !session.existing.contains(&entry.path())
            {
                std::fs::rename(
                    entry.path(),
                    self.directory.join(&session.id).join(entry.file_name()),
                )
                .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
            }
        }
        Ok(())
    }

    /// Stops active capture before engine shutdown; artifacts remain in the Pod for recovery.
    pub async fn shutdown(&self) {
        let mut current = self.session.lock().await;
        if let Some(session) = current.as_mut()
            && let Err(status) = self.stop_locked(session).await
        {
            tracing::warn!(%status, "could not stop profiler before engine shutdown");
        }
    }
}

/// Builds operator-only routes; disabled profiling reports HTTP 501 rather than starting an engine utility.
pub fn routes(profiler: Option<Arc<Profiler>>) -> axum::Router {
    use axum::routing::{delete, post};
    match profiler {
        Some(profiler) => axum::Router::new()
            .route("/v1/internal/profile/start", post(start))
            .route("/v1/internal/profile/{id}/stop", post(stop))
            .route("/v1/internal/profile/{id}", delete(remove))
            .with_state(profiler),
        None => axum::Router::new().route(
            "/v1/internal/profile/start",
            post(|| async {
                (
                    StatusCode::NOT_IMPLEMENTED,
                    "profiling is not enabled for this model server",
                )
            }),
        ),
    }
}

/// Internal start request; the benchmark supplies identity and its bounded capture deadline.
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct StartProfile {
    session_id: Uuid,
    duration_seconds: u32,
}

/// Internal result used by the benchmark to collect only files belonging to its capture.
#[derive(Serialize)]
pub struct ProfileFiles {
    session_id: String,
    directory: PathBuf,
    files: Vec<String>,
    expired: bool,
}

/// Starts a single engine-wide capture, rejecting overlapping operators with HTTP 409.
pub async fn start(
    State(profiler): State<Arc<Profiler>>,
    Json(input): Json<StartProfile>,
) -> Result<Json<ProfileFiles>, StatusCode> {
    if input.duration_seconds == 0 {
        return Err(StatusCode::BAD_REQUEST);
    }
    let mut current = profiler.session.lock().await;
    if current.is_some() {
        return Err(StatusCode::CONFLICT);
    }
    let id = input.session_id.to_string();
    let directory = profiler.directory.join(&id);
    std::fs::create_dir(&directory).map_err(|error| {
        if error.kind() == io::ErrorKind::AlreadyExists {
            StatusCode::CONFLICT
        } else {
            StatusCode::INTERNAL_SERVER_ERROR
        }
    })?;
    let existing = std::fs::read_dir(&profiler.directory)
        .and_then(|entries| {
            entries
                .map(|entry| entry.map(|entry| entry.path()))
                .collect()
        })
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    *current = Some(Session {
        id: id.clone(),
        active: true,
        expired: false,
        existing,
    });
    // A lost benchmark process must not leave an unbounded capture. The timer also covers an
    // ambiguous start failure: it waits for the serialized utility call, then attempts stop.
    let weak = Arc::downgrade(&profiler);
    let timeout_id = id.clone();
    tokio::spawn(async move {
        tokio::time::sleep(Duration::from_secs(u64::from(input.duration_seconds))).await;
        if let Some(profiler) = weak.upgrade() {
            let mut current = profiler.session.lock().await;
            if let Some(session) = current.as_mut()
                && session.id == timeout_id
                && session.active
            {
                session.expired = true;
                if let Err(status) = profiler.stop_locked(session).await {
                    tracing::error!(%status, "expired profiler capture could not stop");
                }
            }
        }
    });
    profiler
        .backend
        .profile(true, &id)
        .await
        .map_err(|_| StatusCode::BAD_GATEWAY)?;
    Ok(Json(ProfileFiles {
        session_id: id,
        directory,
        files: Vec::new(),
        expired: false,
    }))
}

/// Flushes one capture and returns its recoverable artifact manifest; repeated stops are safe.
pub async fn stop(
    State(profiler): State<Arc<Profiler>>,
    Path(id): Path<Uuid>,
) -> Result<Json<ProfileFiles>, StatusCode> {
    let mut current = profiler.session.lock().await;
    let session = current
        .as_mut()
        .filter(|session| session.id == id.to_string())
        .ok_or(StatusCode::NOT_FOUND)?;
    profiler.stop_locked(session).await?;
    let directory = profiler.directory.join(&session.id);
    let mut files = std::fs::read_dir(&directory)
        .and_then(|entries| {
            entries
                .map(|entry| entry.map(|entry| entry.file_name().to_string_lossy().into_owned()))
                .collect::<io::Result<Vec<_>>>()
        })
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    files.sort();
    Ok(Json(ProfileFiles {
        session_id: session.id.clone(),
        directory,
        files,
        expired: session.expired,
    }))
}

/// Deletes one stopped capture only after the benchmark has copied its artifacts successfully.
pub async fn remove(
    State(profiler): State<Arc<Profiler>>,
    Path(id): Path<Uuid>,
) -> Result<StatusCode, StatusCode> {
    let mut current = profiler.session.lock().await;
    let session = current
        .as_ref()
        .filter(|session| session.id == id.to_string())
        .ok_or(StatusCode::NOT_FOUND)?;
    if session.active {
        return Err(StatusCode::CONFLICT);
    }
    std::fs::remove_dir_all(profiler.directory.join(&session.id))
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;
    *current = None;
    Ok(StatusCode::NO_CONTENT)
}
