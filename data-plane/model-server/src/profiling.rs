// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Runtime-owned, single-window capture; HTTP clients submit intent, never own timers.

use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tokio::sync::watch;
use tokio::task::JoinHandle;
use tracing::warn;

use crate::backend::Backend;

/// Seconds supplied by the management client, with no deployment-level defaults.
#[derive(Clone, Copy, Debug, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ProfileWindow {
    pub delay_seconds: f64,
    pub duration_seconds: f64,
}

/// Observed runtime stage; stopping includes native trace export.
#[derive(Clone, Copy, Debug, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    Waiting,
    Starting,
    Recording,
    Stopping,
    Completed,
    Cancelled,
    Failed,
}

impl Phase {
    fn terminal(self) -> bool {
        matches!(self, Self::Completed | Self::Cancelled | Self::Failed)
    }
}

/// Latest capture result. Timestamps describe acknowledged controls, not GPU synchronization.
#[derive(Clone, Debug, Serialize)]
pub struct ProfileStatus {
    pub id: String,
    pub phase: Phase,
    pub started_at_unix_ms: Option<u64>,
    pub stop_requested_at_unix_ms: Option<u64>,
    pub finished_at_unix_ms: Option<u64>,
    pub files: Vec<String>,
    pub error: Option<String>,
}

/// Management errors that do not change the running capture.
#[derive(Debug)]
pub enum ProfileError {
    InvalidWindow,
    Conflict,
    NotFound,
    Closed,
}

struct Capture {
    window: ProfileWindow,
    status: watch::Receiver<ProfileStatus>,
    cancel: watch::Sender<bool>,
    task: Option<JoinHandle<()>>,
}

#[derive(Default)]
struct State {
    closed: bool,
    capture: Option<Capture>,
}

/// Owns one engine's active capture and last result until the next accepted submission.
pub struct Profiler {
    backend: Arc<dyn Backend>,
    state: Mutex<State>,
}

impl Profiler {
    /// Creates the supervisor retained by API state and the model-server shutdown path.
    pub fn new(backend: Arc<dyn Backend>) -> Self {
        Self {
            backend,
            state: Mutex::new(State::default()),
        }
    }

    /// Accepts a finite window and returns immediately; the spawned task owns its full lifecycle.
    pub fn submit(&self, id: String, window: ProfileWindow) -> Result<ProfileStatus, ProfileError> {
        let delay = Duration::try_from_secs_f64(window.delay_seconds)
            .map_err(|_| ProfileError::InvalidWindow)?;
        let duration = Duration::try_from_secs_f64(window.duration_seconds)
            .map_err(|_| ProfileError::InvalidWindow)?;
        if duration.is_zero()
            || tokio::time::Instant::now()
                .checked_add(delay)
                .and_then(|at| at.checked_add(duration))
                .is_none()
            || id.len() != 32
            || !id.bytes().all(|byte| byte.is_ascii_hexdigit())
        {
            return Err(ProfileError::InvalidWindow);
        }
        let mut state = self.state.lock().expect("profiler state lock poisoned");
        if state.closed {
            return Err(ProfileError::Closed);
        }
        if let Some(capture) = &state.capture {
            let status = capture.status.borrow();
            if status.id == id && capture.window == window {
                return Ok(status.clone());
            }
            // A failed native control may have left a worker recording. Never overlap or
            // silently reuse that engine; ordinary inference and shutdown remain independent.
            if status.id == id || !status.phase.terminal() || status.phase == Phase::Failed {
                return Err(ProfileError::Conflict);
            }
        }
        let status = ProfileStatus {
            id,
            phase: Phase::Waiting,
            started_at_unix_ms: None,
            stop_requested_at_unix_ms: None,
            finished_at_unix_ms: None,
            files: Vec::new(),
            error: None,
        };
        let (sender, receiver) = watch::channel(status.clone());
        let (cancel, cancelled) = watch::channel(false);
        let backend = self.backend.clone();
        let task = tokio::spawn(capture_window(backend, sender, cancelled, delay, duration));
        state.capture = Some(Capture {
            window,
            status: receiver,
            cancel,
            task: Some(task),
        });
        Ok(status)
    }

    /// Returns the matching snapshot; a stale ID must not observe another client's capture.
    pub fn status(&self, id: &str) -> Result<ProfileStatus, ProfileError> {
        let state = self.state.lock().expect("profiler state lock poisoned");
        let capture = state.capture.as_ref().ok_or(ProfileError::NotFound)?;
        let status = capture.status.borrow();
        if status.id != id {
            return Err(ProfileError::NotFound);
        }
        Ok(status.clone())
    }

    /// Requests cancellation of this ID only; callers query until stop/export is acknowledged.
    pub fn cancel(&self, id: &str) -> Result<ProfileStatus, ProfileError> {
        let state = self.state.lock().expect("profiler state lock poisoned");
        let capture = state.capture.as_ref().ok_or(ProfileError::NotFound)?;
        let status = capture.status.borrow();
        if status.id != id {
            return Err(ProfileError::NotFound);
        }
        capture.cancel.send_replace(true);
        Ok(status.clone())
    }

    /// Closes submission and joins cancellation within the process's existing drain budget.
    pub async fn shutdown(&self, timeout: Duration) {
        let task = {
            let mut state = self.state.lock().expect("profiler state lock poisoned");
            state.closed = true;
            state.capture.as_mut().and_then(|capture| {
                capture.cancel.send_replace(true);
                capture.task.take()
            })
        };
        if let Some(mut task) = task {
            match tokio::time::timeout(timeout, &mut task).await {
                Ok(Ok(())) => {}
                Ok(Err(error)) => warn!(%error, "profiling task failed during shutdown"),
                Err(_) => {
                    warn!("profiling stop was not confirmed before process drain deadline");
                    task.abort();
                    let _ = task.await;
                }
            }
        }
    }
}

/// Serializes native start/stop even if cancellation arrives while an engine call is pending.
async fn capture_window(
    backend: Arc<dyn Backend>,
    sender: watch::Sender<ProfileStatus>,
    mut cancel: watch::Receiver<bool>,
    delay: Duration,
    duration: Duration,
) {
    let mut status = sender.borrow().clone();
    let prefix = format!("foretoken_{}", status.id);
    let cancelled = tokio::select! {
        biased;
        _ = cancel.wait_for(|cancelled| *cancelled) => true,
        _ = tokio::time::sleep(delay) => false,
    };
    if cancelled {
        status.phase = Phase::Cancelled;
        status.finished_at_unix_ms = Some(unix_ms());
        sender.send_replace(status);
        return;
    }
    status.phase = Phase::Starting;
    sender.send_replace(status.clone());
    let started = backend.start_profile(&prefix).await;
    let cancelled = if let Err(error) = started {
        status.error = Some(format!("start failed: {error}"));
        false
    } else {
        status.phase = Phase::Recording;
        status.started_at_unix_ms = Some(unix_ms());
        sender.send_replace(status.clone());
        tokio::select! {
            biased;
            _ = cancel.wait_for(|cancelled| *cancelled) => true,
            _ = tokio::time::sleep(duration) => false,
        }
    };
    // Start may have reached only some workers before failing. Always request stop;
    // never cancel an in-flight native call merely because its HTTP client went away.
    status.phase = Phase::Stopping;
    status.stop_requested_at_unix_ms = Some(unix_ms());
    sender.send_replace(status.clone());
    match backend.stop_profile(&prefix).await {
        Ok(files) => status.files = files,
        Err(error) => {
            let detail = format!("stop/export failed: {error}");
            status.error = Some(match status.error {
                Some(start) => format!("{start}; {detail}"),
                None => detail,
            });
        }
    }
    status.phase = if status.error.is_some() {
        Phase::Failed
    } else if cancelled {
        Phase::Cancelled
    } else {
        Phase::Completed
    };
    status.finished_at_unix_ms = Some(unix_ms());
    sender.send_replace(status);
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock precedes Unix epoch")
        .as_millis()
        .try_into()
        .expect("Unix milliseconds fit u64")
}
