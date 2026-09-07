// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Reports the capacity of the RuntimeCache filesystem before the model is ready.

use std::path::{Path, PathBuf};
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

#[derive(Clone)]
pub struct Config {
    mount_path: PathBuf,
    pod_uid: String,
    observation_port: u16,
    minimum_available_bytes: u64,
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
        let minimum_available_bytes = std::env::var("FORETOKEN_CACHE_MIN_AVAILABLE_BYTES")
            .unwrap_or_else(|_| "0".into())
            .parse()
            .map_err(|_| "FORETOKEN_CACHE_MIN_AVAILABLE_BYTES must be a non-negative integer")?;
        Ok(Some(Self {
            mount_path,
            pod_uid,
            observation_port,
            minimum_available_bytes,
        }))
    }

    /// Returns the controller-selected private observation port.
    pub fn observation_port(&self) -> u16 {
        self.observation_port
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

/// Waits for the controller-maintained free-space reserve before starting model download and load.
pub async fn wait_until_ready(config: &Config) -> Result<(), String> {
    while filesystem_capacity(config)?.1 < config.minimum_available_bytes {
        tokio::time::sleep(Duration::from_secs(2)).await;
    }
    Ok(())
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
