// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Frontend process configuration.

use std::env;
use std::path::PathBuf;
use std::time::Duration;

use foretoken_router::RouterAlgorithm;

const SERVING_SNAPSHOT_ENV: &str = "FORETOKEN_SERVING_SNAPSHOT";
const LISTEN_ADDRESS_ENV: &str = "FORETOKEN_LISTEN_ADDRESS";
const STREAM_IDLE_SECONDS_ENV: &str = "FORETOKEN_STREAM_IDLE_SECONDS";
const KV_INDEX_KEY_PATH_ENV: &str = "FORETOKEN_KV_INDEX_KEY_PATH";
const ROUTER_ALGORITHM_ENV: &str = "FORETOKEN_ROUTER_ALGORITHM";

pub(crate) struct RuntimeConfig {
    pub(crate) serving_snapshot: PathBuf,
    pub(crate) listen_address: String,
    pub(crate) stream_idle: Duration,
    pub(crate) router_algorithm: RouterAlgorithm,
}

impl RuntimeConfig {
    pub(crate) fn from_env() -> Result<Self, String> {
        Ok(Self {
            serving_snapshot: required_path(SERVING_SNAPSHOT_ENV)?,
            listen_address: required_env(LISTEN_ADDRESS_ENV)?,
            stream_idle: required_positive_duration(STREAM_IDLE_SECONDS_ENV)?,
            router_algorithm: env::var(ROUTER_ALGORITHM_ENV)
                .unwrap_or_else(|_| RouterAlgorithm::default().as_str().into())
                .parse()?,
        })
    }
}

fn required_env(name: &str) -> Result<String, String> {
    env::var(name).map_err(|_| format!("{name} must be set by the frontend controller"))
}

fn required_positive_duration(name: &str) -> Result<Duration, String> {
    let seconds = required_env(name)?
        .parse::<u64>()
        .map_err(|_| format!("{name} must be a positive integer"))?;
    if seconds == 0 {
        return Err(format!("{name} must be a positive integer"));
    }
    Ok(Duration::from_secs(seconds))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum KvIndexKeyError {
    ReadFailed,
    InvalidLength,
}

// KV indexing remains a soft hint, but a configured credential must never fail silently.
pub(crate) fn kv_index_key() -> Result<Option<[u8; 32]>, KvIndexKeyError> {
    let Ok(path) = env::var(KV_INDEX_KEY_PATH_ENV) else {
        return Ok(None);
    };
    let bytes = std::fs::read(path).map_err(|_| KvIndexKeyError::ReadFailed)?;
    bytes
        .as_slice()
        .try_into()
        .map(Some)
        .map_err(|_| KvIndexKeyError::InvalidLength)
}

fn required_path(name: &str) -> Result<PathBuf, String> {
    let value = required_env(name)?;
    if value.is_empty() {
        return Err(format!("{name} must not be empty"));
    }
    Ok(value.into())
}
