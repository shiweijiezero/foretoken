// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Environment boundary for the controller-owned typed launch plan.
//!
//! The launch plan is read as a raw envelope: an engine discriminator plus the
//! unparsed JSON payload. The selected engine adapter parses the payload.

use std::net::SocketAddr;
use std::str::FromStr;

use serde::Deserialize;

use crate::engine::EngineKind;

const LAUNCH_PLAN_ENV: &str = "FORETOKEN_LAUNCH_PLAN";
const LEGACY_LAUNCH_PLAN_ENV: &str = "FORETOKEN_VLLM_LAUNCH_PLAN";
const LISTEN_ENV: &str = "FORETOKEN_INTERNAL_LISTEN";

/// Engine-neutral launch plan envelope: the discriminator plus the raw payload
/// that the selected engine adapter parses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LaunchPlan {
    pub kind: EngineKind,
    pub payload: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeConfig {
    pub launch: LaunchPlan,
    pub listen_address: SocketAddr,
}

impl RuntimeConfig {
    pub fn from_env() -> Result<Self, String> {
        let payload = launch_plan_env()?;
        let launch = LaunchPlan::parse(&payload)?;
        let listen_address = required_env(LISTEN_ENV)?
            .parse()
            .map_err(|_| format!("{LISTEN_ENV} must be a socket address"))?;
        Ok(Self {
            launch,
            listen_address,
        })
    }
}

impl LaunchPlan {
    pub fn parse(payload: &str) -> Result<Self, String> {
        let kind = probe_kind(payload)?;
        Ok(Self {
            kind,
            payload: payload.to_owned(),
        })
    }
}

/// Probe used to read only the `kind` field without parsing the full payload.
#[derive(Deserialize)]
struct KindProbe {
    kind: Option<String>,
}

fn probe_kind(payload: &str) -> Result<EngineKind, String> {
    let probe: KindProbe =
        serde_json::from_str(payload).map_err(|error| format!("invalid launch plan: {error}"))?;
    // Legacy plans omit `kind` and are vLLM by construction.
    match probe.kind.as_deref() {
        None => Ok(EngineKind::Vllm),
        Some(value) => EngineKind::from_str(value),
    }
}

/// Resolves the launch plan from `FORETOKEN_LAUNCH_PLAN`, falling back to the
/// legacy `FORETOKEN_VLLM_LAUNCH_PLAN` so older control planes keep working.
fn launch_plan_env() -> Result<String, String> {
    match std::env::var(LAUNCH_PLAN_ENV) {
        Ok(value) if !value.is_empty() => Ok(value),
        Ok(_) => Err(format!("{LAUNCH_PLAN_ENV} must not be empty")),
        Err(std::env::VarError::NotPresent) => match std::env::var(LEGACY_LAUNCH_PLAN_ENV) {
            Ok(value) if !value.is_empty() => Ok(value),
            Ok(_) => Err(format!("{LEGACY_LAUNCH_PLAN_ENV} must not be empty")),
            Err(std::env::VarError::NotPresent) => Err(format!(
                "{LAUNCH_PLAN_ENV} or {LEGACY_LAUNCH_PLAN_ENV} must be set by the ModelGroup controller"
            )),
            Err(std::env::VarError::NotUnicode(_)) => {
                Err(format!("{LEGACY_LAUNCH_PLAN_ENV} must be valid Unicode"))
            }
        },
        Err(std::env::VarError::NotUnicode(_)) => {
            Err(format!("{LAUNCH_PLAN_ENV} must be valid Unicode"))
        }
    }
}

fn required_env(name: &str) -> Result<String, String> {
    match std::env::var(name) {
        Ok(value) if !value.is_empty() => Ok(value),
        Ok(_) | Err(std::env::VarError::NotPresent) => {
            Err(format!("{name} must be set by the ModelGroup controller"))
        }
        Err(std::env::VarError::NotUnicode(_)) => Err(format!("{name} must be valid Unicode")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_defaults_missing_kind_to_vllm() {
        let plan = LaunchPlan::parse(r#"{"version":1,"nodeCount":1}"#).expect("legacy plan");
        assert_eq!(plan.kind, EngineKind::Vllm);
        assert_eq!(plan.payload, r#"{"version":1,"nodeCount":1}"#);
    }

    #[test]
    fn parse_reads_explicit_kind() {
        let plan = LaunchPlan::parse(r#"{"kind":"vllm","version":1}"#).expect("vllm plan");
        assert_eq!(plan.kind, EngineKind::Vllm);

        let plan = LaunchPlan::parse(r#"{"kind":"sglang","version":1}"#).expect("sglang plan");
        assert_eq!(plan.kind, EngineKind::Sglang);
    }

    #[test]
    fn parse_rejects_unknown_kind() {
        assert!(LaunchPlan::parse(r#"{"kind":"tensorrt","version":1}"#).is_err());
    }

    #[test]
    fn parse_rejects_malformed_json() {
        assert!(LaunchPlan::parse("not json").is_err());
    }
}
