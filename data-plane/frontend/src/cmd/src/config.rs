// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Frontend listener, configuration source, and process shutdown settings.

use std::env;
use std::path::PathBuf;
use std::time::Duration;

const SERVING_SNAPSHOT_ENV: &str = "FORETOKEN_SERVING_SNAPSHOT";
const LISTEN_ADDRESS_ENV: &str = "FORETOKEN_LISTEN_ADDRESS";
const DRAIN_SECONDS_ENV: &str = "FORETOKEN_DRAIN_SECONDS";
const KV_INDEX_KEY_PATH_ENV: &str = "FORETOKEN_KV_INDEX_KEY_PATH";

pub(crate) struct RuntimeConfig {
    pub(crate) frontend_uid: String,
    pub(crate) pod_uid: String,
    pub(crate) serving_snapshot: PathBuf,
    pub(crate) listen_address: String,
    pub(crate) drain: Duration,
}

impl RuntimeConfig {
    /// Loads process initialization settings; live request rules come from the serving snapshot.
    pub(crate) fn from_env() -> Result<Self, String> {
        let serving_snapshot = required_env(SERVING_SNAPSHOT_ENV)?;
        if serving_snapshot.is_empty() {
            return Err(format!("{SERVING_SNAPSHOT_ENV} must not be empty"));
        }
        let seconds = required_env(DRAIN_SECONDS_ENV)?
            .parse::<u64>()
            .map_err(|_| format!("{DRAIN_SECONDS_ENV} must be a positive integer"))?;
        if seconds == 0 {
            return Err(format!("{DRAIN_SECONDS_ENV} must be a positive integer"));
        }
        Ok(Self {
            frontend_uid: required_env("FORETOKEN_FRONTEND_UID")?,
            pod_uid: required_env("FORETOKEN_POD_UID")?,
            serving_snapshot: serving_snapshot.into(),
            listen_address: required_env(LISTEN_ADDRESS_ENV)?,
            drain: Duration::from_secs(seconds),
        })
    }
}

fn required_env(name: &str) -> Result<String, String> {
    env::var(name).map_err(|_| format!("{name} must be set by the frontend controller"))
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum KvIndexKeyError {
    ReadFailed,
    InvalidLength,
}

/// Loads the optional KV-index credential for runtime-builder startup.
/// Unreadable configured credentials remain visible as degraded routing state.
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
