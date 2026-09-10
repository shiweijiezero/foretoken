// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Runtime-owned, bounded Torch capture and durable artifact publication.

use std::collections::HashMap;
use std::fs::{self, File};
use std::io::{self, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use serde::{Deserialize, Serialize};
use tokio::sync::{mpsc, watch};
use tokio::task::JoinHandle;
use uuid::Uuid;

use crate::api::RuntimeHealth;
use crate::backend::VllmBackend;

const ARTIFACT_ROOT: &str = "/var/lib/foretoken/profiles";
const START_TIMEOUT: Duration = Duration::from_secs(30);
const STOP_TIMEOUT: Duration = Duration::from_secs(120);

/// Startup-only diagnostic binding projected by the workload controller.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Config {
    pub claim: String,
    pub pod_uid: String,
    pub group_uid: String,
    pub runtime_id: String,
    pub workers: usize,
}

impl Config {
    /// Reads an optional platform binding; a fresh identity distinguishes process replacements.
    pub fn from_env(workers: usize) -> Result<Option<Self>, String> {
        let claim = match std::env::var("FORETOKEN_PROFILE_ARTIFACT_CLAIM") {
            Ok(value) if !value.is_empty() => value,
            Ok(_) | Err(std::env::VarError::NotPresent) => return Ok(None),
            Err(error) => return Err(error.to_string()),
        };
        let required = |name: &str| {
            std::env::var(name).map_err(|_| format!("{name} is required for diagnostic capture"))
        };
        Ok(Some(Self {
            claim,
            pod_uid: required("FORETOKEN_POD_UID")?,
            group_uid: required("FORETOKEN_MODEL_GROUP_UID")?,
            runtime_id: Uuid::new_v4().to_string(),
            workers,
        }))
    }

    fn staging(&self) -> PathBuf {
        Path::new(ARTIFACT_ROOT)
            .join(".staging")
            .join(&self.runtime_id)
    }

    /// Prepares this process's isolated staging directory before the inference engine starts.
    pub fn prepare(&self) -> io::Result<()> {
        // The mount must already exist; never silently substitute Pod-local storage.
        fs::metadata(ARTIFACT_ROOT)?;
        fs::create_dir_all(Path::new(ARTIFACT_ROOT).join("runs"))?;
        fs::create_dir_all(self.staging())?;
        File::open(ARTIFACT_ROOT)?.sync_all()
    }

    /// Renders the native Torch configuration; iteration schedules remain disabled for this path.
    pub fn engine_argument(&self) -> String {
        format!(
            "--profiler-config={}",
            serde_json::json!({
                "profiler": "torch",
                "torch_profiler_dir": self.staging(),
                "torch_profiler_use_gzip": false,
                "torch_profiler_dump_cuda_time_total": false,
                "ignore_frontend": true,
            })
        )
    }
}

/// Actions accepted from the ProfileRun reconciler for one fixed runtime identity.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Deserialize)]
pub enum Action {
    Capture,
    Finish,
    Cancel,
}

/// One control operation; retrying a run never changes its duration or selected runtime.
#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct Request {
    pub run_uid: String,
    pub runtime_id: String,
    pub group_uid: String,
    pub action: Action,
    pub duration_ms: u64,
}

/// Observed capture state returned to the reconciler and stored with sealed artifacts.
#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Record {
    pub run_uid: String,
    pub phase: String,
    pub duration_ms: u64,
    pub started_at_unix_ms: Option<u64>,
    pub stopped_at_unix_ms: Option<u64>,
    pub artifact_dir: Option<String>,
    pub message: String,
}

impl Record {
    fn terminal(&self) -> bool {
        matches!(self.phase.as_str(), "Succeeded" | "Failed" | "Cancelled")
    }
}

/// Runtime identity and the requested run's state; absence means that run has not started here.
#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
pub struct Observation {
    runtime_id: String,
    pod_uid: String,
    group_uid: String,
    artifact_claim: String,
    capture: Option<Record>,
}

struct Entry {
    record: Record,
    action: watch::Sender<Action>,
}

struct State {
    entries: HashMap<String, Entry>,
    active: Option<String>,
}

/// HTTP-side control handle; native work belongs to the process supervisor, not request tasks.
#[derive(Clone)]
pub struct Handle {
    config: Config,
    state: Arc<Mutex<State>>,
    health: Arc<RuntimeHealth>,
    sender: mpsc::UnboundedSender<(String, watch::Receiver<Action>)>,
}

impl Handle {
    /// Returns identity and previously observed state without probing or restarting native capture.
    pub fn observe(&self, uid: Option<&str>) -> Observation {
        let state = self.state.lock().expect("capture state lock poisoned");
        Observation {
            runtime_id: self.config.runtime_id.clone(),
            pod_uid: self.config.pod_uid.clone(),
            group_uid: self.config.group_uid.clone(),
            artifact_claim: self.config.claim.clone(),
            capture: uid.and_then(|uid| state.entries.get(uid).map(|entry| entry.record.clone())),
        }
    }

    /// Accepts an idempotent operation or rejects identity changes and competing capture runs.
    pub fn control(&self, request: Request) -> Result<(), String> {
        Uuid::parse_str(&request.run_uid).map_err(|_| "invalid run UID")?;
        if request.runtime_id != self.config.runtime_id
            || request.group_uid != self.config.group_uid
        {
            return Err("runtime identity changed".into());
        }
        if request.duration_ms == 0 {
            return Err("duration must be positive".into());
        }
        let mut state = self.state.lock().expect("capture state lock poisoned");
        if let Some(entry) = state.entries.get(&request.run_uid) {
            if entry.record.duration_ms != request.duration_ms {
                return Err("capture duration is immutable".into());
            }
            if !entry.record.terminal() && request.action != Action::Capture {
                // Cancellation is sticky: a delayed Finish cannot reverse it.
                if *entry.action.borrow() != Action::Cancel {
                    entry.action.send_replace(request.action);
                }
            }
            return Ok(());
        }
        if request.action == Action::Capture && (state.active.is_some() || !self.health.accepting())
        {
            return Err("runtime is busy or unavailable".into());
        }
        let (action, receiver) = watch::channel(request.action);
        let capture = request.action == Action::Capture;
        let uid = request.run_uid;
        state.entries.insert(
            uid.clone(),
            Entry {
                record: Record {
                    run_uid: uid.clone(),
                    phase: if capture { "Starting" } else { "Cancelled" }.into(),
                    duration_ms: request.duration_ms,
                    started_at_unix_ms: None,
                    stopped_at_unix_ms: None,
                    artifact_dir: None,
                    message: String::new(),
                },
                action,
            },
        );
        if capture {
            state.active = Some(uid.clone());
            self.sender
                .send((uid, receiver))
                .map_err(|_| "capture supervisor stopped")?;
        }
        Ok(())
    }
}

/// Owns native operations through stop/flush; an uncertain operation requires engine termination.
pub struct Supervisor {
    handle: Handle,
    receiver: mpsc::UnboundedReceiver<(String, watch::Receiver<Action>)>,
    backend: Arc<VllmBackend>,
    native: Option<JoinHandle<Result<(), String>>>,
    publication: Option<JoinHandle<io::Result<Record>>>,
}

impl Supervisor {
    /// Creates shared API state and the single supervisor driven by model-server's main loop.
    pub fn new(config: Config, backend: Arc<VllmBackend>, health: Arc<RuntimeHealth>) -> Self {
        let (sender, receiver) = mpsc::unbounded_channel();
        Self {
            handle: Handle {
                config,
                health,
                sender,
                state: Arc::new(Mutex::new(State {
                    entries: HashMap::new(),
                    active: None,
                })),
            },
            receiver,
            backend,
            native: None,
            publication: None,
        }
    }

    /// Clones only the request-facing state, retaining native-task ownership in this supervisor.
    pub fn handle(&self) -> Handle {
        self.handle.clone()
    }

    /// Runs serialized captures until shutdown or an uncertain native operation needs escalation.
    pub async fn run(&mut self) -> Result<(), String> {
        while let Some((uid, action)) = self.receiver.recv().await {
            self.capture(&uid, action).await?;
        }
        Ok(())
    }

    fn update(&self, uid: &str, update: impl FnOnce(&mut Record)) {
        let mut state = self
            .handle
            .state
            .lock()
            .expect("capture state lock poisoned");
        let entry = state
            .entries
            .get_mut(uid)
            .expect("supervised capture exists");
        update(&mut entry.record);
        if entry.record.terminal() {
            state.active = None;
        }
    }

    async fn native_operation(&mut self, start: bool) -> Result<(), String> {
        let backend = self.backend.clone();
        self.native = Some(tokio::spawn(
            async move { backend.set_profiling(start).await },
        ));
        let budget = if start { START_TIMEOUT } else { STOP_TIMEOUT };
        let result = tokio::time::timeout(
            budget,
            self.native.as_mut().expect("native operation exists"),
        )
        .await;
        match result {
            Ok(result) => {
                self.native = None;
                result.map_err(|error| error.to_string())?
            }
            Err(_) => Err(format!(
                "native profiling {} timed out",
                if start { "start" } else { "stop" }
            )),
        }
    }

    // Native utilities run without the state lock. HTTP cancellation only changes desired action;
    // it cannot drop the in-flight operation or its independently owned deadline.
    async fn capture(
        &mut self,
        uid: &str,
        mut action: watch::Receiver<Action>,
    ) -> Result<(), String> {
        let staging = self.handle.config.staging();
        let prepare = fs::create_dir_all(&staging).and_then(|()| {
            if fs::read_dir(&staging)?.next().is_some() {
                Err(io::Error::other(
                    "unsealed output remains in the runtime staging directory",
                ))
            } else {
                Ok(())
            }
        });
        if let Err(error) = prepare {
            self.update(uid, |record| {
                record.phase = "Failed".into();
                record.message = error.to_string();
            });
            return Ok(());
        }
        if *action.borrow() != Action::Capture {
            self.update(uid, |record| record.phase = "Cancelled".into());
            return Ok(());
        }
        self.native_operation(true).await?;
        self.update(uid, |record| {
            record.phase = "Capturing".into();
            record.started_at_unix_ms = Some(now_ms());
        });
        let duration = self
            .handle
            .observe(Some(uid))
            .capture
            .expect("capture exists")
            .duration_ms;
        if *action.borrow_and_update() == Action::Capture {
            tokio::select! {
                _ = tokio::time::sleep(Duration::from_millis(duration)) => {},
                _ = action.changed() => {},
            }
        }
        self.update(uid, |record| record.phase = "Stopping".into());
        self.native_operation(false).await?;
        self.update(uid, |record| record.stopped_at_unix_ms = Some(now_ms()));
        let mut record = self
            .handle
            .observe(Some(uid))
            .capture
            .expect("capture exists");
        let cancelled = *action.borrow() == Action::Cancel;
        record.phase = if cancelled { "Cancelled" } else { "Succeeded" }.into();
        let config = self.handle.config.clone();
        self.publication = Some(tokio::task::spawn_blocking(move || {
            seal(&config, record, cancelled)
        }));
        let publication = self.publication.as_mut().expect("publication exists").await;
        self.publication = None;
        match publication {
            Ok(Ok(record)) => self.update(uid, |current| *current = record),
            result => self.update(uid, |record| {
                record.phase = "Failed".into();
                record.message = match result {
                    Ok(Err(error)) => error.to_string(),
                    Err(error) => error.to_string(),
                    Ok(Ok(_)) => unreachable!(),
                };
            }),
        }
        Ok(())
    }

    /// Cancels a pending native task only after main has confirmed engine process exit.
    pub async fn engine_stopped(&mut self, reason: &str) {
        if let Some(task) = self.native.take() {
            task.abort();
            let _ = task.await;
        }
        if let Some(publication) = self.publication.take()
            && let Ok(Ok(record)) = publication.await
        {
            let uid = record.run_uid.clone();
            self.update(&uid, |current| *current = record);
        }
        let uid = self
            .handle
            .state
            .lock()
            .expect("capture state lock poisoned")
            .active
            .clone();
        if let Some(uid) = uid {
            self.update(&uid, |record| {
                record.phase = "Failed".into();
                record.message = reason.into();
                record.stopped_at_unix_ms = Some(now_ms());
            });
        }
        // Staging is deliberately retained on engine failure for storage-owner diagnosis.
    }
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock precedes Unix epoch")
        .as_millis() as u64
}

// The supported adapter emits one uncompressed Chrome trace per worker. Validate its contents
// after every native worker has flushed, then rename the entire directory, including nested output.
fn seal(config: &Config, mut record: Record, cancelled: bool) -> io::Result<Record> {
    let staging = config.staging();
    if !cancelled {
        let mut traces = 0;
        for entry in fs::read_dir(&staging)? {
            let path = entry?.path();
            if path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| name.ends_with(".pt.trace.json"))
            {
                let file = File::open(path)?;
                let trace: TorchTrace = serde_json::from_reader(BufReader::new(&file))?;
                if !trace.events.0 {
                    return Err(io::Error::other(
                        "Torch trace contains no GPU kernel activity",
                    ));
                }
                file.sync_all()?;
                traces += 1;
            }
        }
        if traces != config.workers {
            return Err(io::Error::other(format!(
                "expected {} worker traces, received {traces}",
                config.workers
            )));
        }
    }
    let relative = format!("runs/{}/{}", record.run_uid, config.runtime_id);
    record.artifact_dir = Some(relative.clone());
    let mut manifest = File::create(staging.join("manifest.json"))?;
    serde_json::to_writer(&mut manifest, &record)?;
    manifest.write_all(b"\n")?;
    manifest.sync_all()?;
    File::open(&staging)?.sync_all()?;
    let destination = Path::new(ARTIFACT_ROOT).join(relative);
    fs::create_dir_all(destination.parent().expect("run directory has parent"))?;
    fs::rename(&staging, &destination)?;
    File::open(destination.parent().expect("run directory has parent"))?.sync_all()?;
    File::open(Path::new(ARTIFACT_ROOT).join("runs"))?.sync_all()?;
    Ok(record)
}

#[derive(Deserialize)]
struct TorchTrace {
    #[serde(rename = "traceEvents")]
    events: KernelEvents,
}

struct KernelEvents(bool);

// Consume large traces one event at a time; loading an entire trace would compete with inference memory.
impl<'de> Deserialize<'de> for KernelEvents {
    fn deserialize<D: serde::Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        struct EventsVisitor;
        impl<'de> serde::de::Visitor<'de> for EventsVisitor {
            type Value = KernelEvents;
            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a Chrome trace event array")
            }
            fn visit_seq<A: serde::de::SeqAccess<'de>>(
                self,
                mut sequence: A,
            ) -> Result<Self::Value, A::Error> {
                #[derive(Deserialize)]
                struct Event {
                    cat: Option<String>,
                }
                let mut kernel = false;
                while let Some(event) = sequence.next_element::<Event>()? {
                    kernel |= event.cat.as_deref() == Some("kernel");
                }
                Ok(KernelEvents(kernel))
            }
        }
        deserializer.deserialize_seq(EventsVisitor)
    }
}
