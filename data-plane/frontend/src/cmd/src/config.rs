// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Frontend process configuration.

use std::env;
use std::path::PathBuf;
use std::time::Duration;

use foretoken_router::{FilterAlgorithm, PickerAlgorithm, RouterPipelineConfig, ScorerAlgorithm};

const SERVING_SNAPSHOT_ENV: &str = "FORETOKEN_SERVING_SNAPSHOT";
const LISTEN_ADDRESS_ENV: &str = "FORETOKEN_LISTEN_ADDRESS";
const REQUEST_TIMEOUT_SECONDS_ENV: &str = "FORETOKEN_REQUEST_TIMEOUT_SECONDS";
const STREAM_IDLE_SECONDS_ENV: &str = "FORETOKEN_STREAM_IDLE_SECONDS";
const KV_INDEX_KEY_PATH_ENV: &str = "FORETOKEN_KV_INDEX_KEY_PATH";
const ROUTER_FILTER_ENV: &str = "FORETOKEN_ROUTER_FILTER";
const ROUTER_SCORER_ENV: &str = "FORETOKEN_ROUTER_SCORER";
const ROUTER_PICKER_ENV: &str = "FORETOKEN_ROUTER_PICKER";
pub(crate) struct RuntimeConfig {
    pub(crate) serving_snapshot: PathBuf,
    pub(crate) listen_address: String,
    pub(crate) request_timeout: Duration,
    pub(crate) stream_idle: Duration,
    pub(crate) router_pipeline: RouterPipelineConfig,
}

impl RuntimeConfig {
    /// Loads the controller-owned frontend settings once during process startup.
    ///
    /// `main` consumes the returned configuration to construct the listener and generation loops;
    /// invalid or absent required values prevent the process from starting.
    pub(crate) fn from_env() -> Result<Self, String> {
        Ok(Self {
            serving_snapshot: required_path(SERVING_SNAPSHOT_ENV)?,
            listen_address: required_env(LISTEN_ADDRESS_ENV)?,
            request_timeout: required_positive_duration(REQUEST_TIMEOUT_SECONDS_ENV)?,
            stream_idle: required_positive_duration(STREAM_IDLE_SECONDS_ENV)?,
            router_pipeline: router_pipeline_from_env(|name| env::var(name))?,
        })
    }
}

/// Resolves the router pipeline selected for this frontend process.
///
/// Startup calls this through [`RuntimeConfig::from_env`]; it returns a validated configuration
/// that is retained by the runtime builder for every snapshot generation.
pub(crate) fn router_pipeline_from_env(
    get_env: impl Fn(&str) -> Result<String, env::VarError>,
) -> Result<RouterPipelineConfig, String> {
    let pipeline = RouterPipelineConfig {
        filter: optional_algorithm(&get_env, ROUTER_FILTER_ENV, FilterAlgorithm::default())?,
        scorer: optional_algorithm(&get_env, ROUTER_SCORER_ENV, ScorerAlgorithm::default())?,
        picker: optional_algorithm(&get_env, ROUTER_PICKER_ENV, PickerAlgorithm::default())?,
    };
    pipeline.validate().map_err(|error| error.to_string())?;
    Ok(pipeline)
}

fn optional_algorithm<T>(
    get_env: &impl Fn(&str) -> Result<String, env::VarError>,
    name: &str,
    default: T,
) -> Result<T, String>
where
    T: std::str::FromStr,
    T::Err: std::fmt::Display,
{
    match get_env(name) {
        Ok(value) => value.parse().map_err(|error: T::Err| error.to_string()),
        Err(env::VarError::NotPresent) => Ok(default),
        Err(env::VarError::NotUnicode(_)) => Err(format!("{name} must be valid UTF-8")),
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

/// Loads the optional KV-index credential for runtime-builder startup.
///
/// The frontend process converts the result into enabled, disabled, or degraded routing state;
/// unreadable configured credentials remain visible as errors rather than silently disabling KV hints.
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
