// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Reports mounted RuntimeCache filesystem capacity.

use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use axum::extract::State;
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use serde::Serialize;
use tokio::net::TcpListener;
use tokio::sync::Notify;
use tracing::warn;

const OBSERVATION_VERSION: u8 = 1;
const WRITE_PROBE_INTERVAL: Duration = Duration::from_secs(2);
const TEMPORARY_CACHE_ROOT: &str = "/tmp/foretoken-runtime-cache";
const CACHE_ENV: [&str; 4] = [
    "HF_HOME",
    "VLLM_CACHE_ROOT",
    "TORCHINDUCTOR_CACHE_DIR",
    "TRITON_CACHE_DIR",
];

/// Selects persistent storage or the Pod-scoped temporary retry cache.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Persistent,
    Temporary,
}

impl Mode {
    /// Returns the stable mode label used by logs and metrics.
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Persistent => "persistent",
            Self::Temporary => "temporary",
        }
    }
}

#[derive(Clone)]
pub struct Config {
    mount_path: PathBuf,
    model_root: PathBuf,
    cache_directories: Vec<(&'static str, PathBuf)>,
    temporary_root: PathBuf,
    pod_uid: String,
    observation_port: u16,
    temporary: Arc<AtomicBool>,
}

#[derive(Serialize)]
struct Observation {
    version: u8,
    pod_uid: String,
    capacity_bytes: u64,
    available_bytes: u64,
}

impl Config {
    /// Reads the controller-projected cache mount and Pod identity, if a RuntimeCache is mounted.
    pub fn from_env() -> Result<Option<Self>, String> {
        let Some(mount_path) = std::env::var_os("FORETOKEN_CACHE_MOUNT_PATH") else {
            return Ok(None);
        };
        let mount_path = PathBuf::from(mount_path);
        if !mount_path.is_absolute() || mount_path == Path::new("/") {
            return Err("FORETOKEN_CACHE_MOUNT_PATH must be an absolute non-root path".into());
        }
        let model_root = std::env::var_os(foretoken_model_files::MODEL_ROOT_ENV)
            .map(PathBuf::from)
            .ok_or("FORETOKEN_MODEL_ROOT must be set when a RuntimeCache is mounted")?;
        if !model_root.is_absolute() {
            return Err("FORETOKEN_MODEL_ROOT must be absolute".into());
        }
        // Relocate only directories projected beneath this data root. Provider-specific
        // paths outside it keep their own owner and are not part of the temporary retry.
        let cache_directories = CACHE_ENV
            .into_iter()
            .filter_map(|name| {
                let path = PathBuf::from(std::env::var_os(name)?);
                let relative = path.strip_prefix(&mount_path).ok()?.to_path_buf();
                Some((name, relative))
            })
            .collect();
        let pod_uid = std::env::var("FORETOKEN_POD_UID")
            .map_err(|_| "FORETOKEN_POD_UID must be set when a RuntimeCache is mounted")?;
        if pod_uid.is_empty() {
            return Err("FORETOKEN_POD_UID must not be empty".into());
        }
        let observation_port = std::env::var("FORETOKEN_CACHE_OBSERVATION_PORT")
            .map_err(
                |_| "FORETOKEN_CACHE_OBSERVATION_PORT must be set when a RuntimeCache is mounted",
            )?
            .parse()
            .map_err(|_| "FORETOKEN_CACHE_OBSERVATION_PORT must be a valid TCP port")?;
        if observation_port == 0 {
            return Err("FORETOKEN_CACHE_OBSERVATION_PORT must not be zero".into());
        }
        Ok(Some(Self {
            mount_path,
            model_root,
            cache_directories,
            temporary_root: Path::new(TEMPORARY_CACHE_ROOT).join(&pod_uid),
            pod_uid,
            observation_port,
            temporary: Arc::new(AtomicBool::new(false)),
        }))
    }

    /// Returns the controller-selected private observation port.
    pub fn observation_port(&self) -> u16 {
        self.observation_port
    }

    /// Resolve a mounted model or tokenizer directory for the engine launcher.
    pub fn local_artifact_path(&self, identifier: &str) -> io::Result<Option<String>> {
        foretoken_model_files::resolve_directory(Some(&self.model_root), identifier)?
            .map(|path| {
                path.into_os_string().into_string().map_err(|_| {
                    io::Error::new(
                        io::ErrorKind::InvalidData,
                        "local artifact path is not UTF-8",
                    )
                })
            })
            .transpose()
    }

    /// Creates the selected cache directories and verifies that the child can write them.
    pub fn prepare(&self, mode: Mode) -> io::Result<()> {
        let root = self.root(mode);
        fs::create_dir_all(root)?;
        for (_, directory) in &self.cache_directories {
            fs::create_dir_all(root.join(directory))?;
        }
        self.probe_writable(mode)
    }

    /// Returns child-process cache overrides for a temporary retry.
    pub fn engine_environment(&self, mode: Mode) -> Vec<(String, String)> {
        if mode == Mode::Persistent {
            return Vec::new();
        }
        self.cache_directories
            .iter()
            .map(|(name, directory)| {
                (
                    (*name).to_owned(),
                    self.root(mode).join(directory).display().to_string(),
                )
            })
            .collect()
    }

    /// Verifies that the selected cache root accepts a durable write and cleanup.
    pub fn probe_writable(&self, mode: Mode) -> io::Result<()> {
        let probe = self
            .root(mode)
            .join(format!(".foretoken-write-probe-{}", self.pod_uid));
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(&probe)?;
        file.write_all(&[0])?;
        file.sync_data()?;
        drop(file);
        fs::remove_file(probe)
    }

    /// Waits until the selected cache no longer accepts the write probe.
    pub async fn wait_until_unwritable(&self, mode: Mode) -> io::Error {
        loop {
            tokio::time::sleep(WRITE_PROBE_INTERVAL).await;
            if let Err(error) = self.probe_writable(mode) {
                return error;
            }
        }
    }

    /// Publishes the active child-process cache mode to metrics consumers.
    pub fn set_mode(&self, mode: Mode) {
        self.temporary
            .store(mode == Mode::Temporary, Ordering::Release);
    }

    fn root(&self, mode: Mode) -> &Path {
        match mode {
            Mode::Persistent => &self.mount_path,
            Mode::Temporary => &self.temporary_root,
        }
    }

    /// Renders RuntimeCache filesystem metrics for the model-server scrape endpoint.
    pub fn render_openmetrics(&self) -> String {
        let measurement = filesystem_capacity(self);
        if let Err(error) = &measurement {
            warn!(path = %self.mount_path.display(), %error, "could not inspect RuntimeCache filesystem for metrics");
        }
        let mut output = format!(
            "# HELP foretoken_runtime_cache_observation_success Whether the RuntimeCache filesystem could be inspected.\n\
             # TYPE foretoken_runtime_cache_observation_success gauge\n\
             foretoken_runtime_cache_observation_success {}\n\
             # HELP foretoken_runtime_cache_temporary Whether EngineCore is using Pod-scoped temporary cache storage.\n\
             # TYPE foretoken_runtime_cache_temporary gauge\n\
             foretoken_runtime_cache_temporary {}\n",
            u8::from(measurement.is_ok()),
            u8::from(self.temporary.load(Ordering::Acquire))
        );
        if let Ok((capacity_bytes, available_bytes)) = measurement {
            output.push_str("# HELP foretoken_runtime_cache_capacity_bytes RuntimeCache filesystem capacity in bytes.\n# TYPE foretoken_runtime_cache_capacity_bytes gauge\n");
            output.push_str(&format!(
                "foretoken_runtime_cache_capacity_bytes {capacity_bytes}\n"
            ));
            output.push_str("# HELP foretoken_runtime_cache_available_bytes RuntimeCache filesystem bytes available to the model-server.\n# TYPE foretoken_runtime_cache_available_bytes gauge\n");
            output.push_str(&format!(
                "foretoken_runtime_cache_available_bytes {available_bytes}\n"
            ));
        }
        output
    }
}

/// Serves filesystem observations on the private cache port until the model-server shuts down.
pub async fn serve(
    listener: TcpListener,
    config: Config,
    shutdown: std::sync::Arc<Notify>,
) -> std::io::Result<()> {
    let app = Router::new()
        .route("/v1/internal/cache", get(observe))
        .with_state(config);
    axum::serve(listener, app)
        .with_graceful_shutdown(async move { shutdown.notified().await })
        .await
}

async fn observe(State(config): State<Config>) -> Result<Json<Observation>, StatusCode> {
    let (capacity_bytes, available_bytes) = filesystem_capacity(&config).map_err(|error| {
        warn!(path = %config.mount_path.display(), %error, "could not inspect RuntimeCache filesystem");
        StatusCode::SERVICE_UNAVAILABLE
    })?;
    Ok(Json(Observation {
        version: OBSERVATION_VERSION,
        pod_uid: config.pod_uid,
        capacity_bytes,
        available_bytes,
    }))
}

fn filesystem_capacity(config: &Config) -> Result<(u64, u64), String> {
    let stats = rustix::fs::statvfs(&config.mount_path).map_err(|error| error.to_string())?;
    let block_size = stats.f_frsize;
    let capacity_bytes = stats
        .f_blocks
        .checked_mul(block_size)
        .ok_or_else(|| "RuntimeCache capacity exceeds the supported byte range".to_string())?;
    let available_bytes = stats
        .f_bavail
        .checked_mul(block_size)
        .ok_or_else(|| "RuntimeCache availability exceeds the supported byte range".to_string())?;
    Ok((capacity_bytes, available_bytes))
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;
    use std::sync::Arc;
    use std::sync::atomic::AtomicBool;
    use std::time::{SystemTime, UNIX_EPOCH};

    use super::{Config, Mode};

    // A concrete persistent write failure must leave the PVC untouched and prepare the Pod cache.
    #[test]
    fn temporary_cache_is_writable_when_persistent_root_is_not_a_directory() {
        let identity = format!(
            "foretoken-runtime-cache-{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        );
        let root = std::env::temp_dir().join(identity);
        std::fs::create_dir_all(&root).unwrap();
        let persistent = root.join("persistent");
        std::fs::write(&persistent, b"not a directory").unwrap();
        let temporary = root.join("temporary");
        let config = Config {
            mount_path: persistent.clone(),
            model_root: persistent.join("models"),
            cache_directories: vec![("HF_HOME", PathBuf::from("models"))],
            temporary_root: temporary.clone(),
            pod_uid: "pod".into(),
            observation_port: 9001,
            temporary: Arc::new(AtomicBool::new(false)),
        };

        assert!(config.prepare(Mode::Persistent).is_err());
        config.prepare(Mode::Temporary).unwrap();
        let environment = config.engine_environment(Mode::Temporary);
        assert!(
            environment
                .iter()
                .all(|(_, path)| path.starts_with(temporary.to_str().unwrap()))
        );
        assert_eq!(std::fs::read(&persistent).unwrap(), b"not a directory");
        config.set_mode(Mode::Temporary);
        assert!(
            config
                .render_openmetrics()
                .contains("foretoken_runtime_cache_temporary 1\n")
        );

        std::fs::remove_dir_all(root).unwrap();
    }
}
