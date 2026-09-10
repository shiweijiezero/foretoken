// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Versioned launch contract for the SGLang adapter.

use std::collections::HashSet;

use serde::Deserialize;

use crate::runtime_transport::LOOPBACK_HOST;

fn default_tp() -> usize {
    1
}

fn default_dp() -> usize {
    1
}

fn default_body_limit() -> usize {
    64 * 1024 * 1024
}

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SglangLaunchPlan {
    pub version: u8,
    /// Model path served by SGLang.
    pub model: String,
    /// Optional model revision.
    #[serde(default)]
    pub revision: Option<String>,
    /// Tensor-parallel size.
    #[serde(default = "default_tp")]
    pub tp: usize,
    /// Data-parallel size.
    #[serde(default = "default_dp")]
    pub dp: usize,
    /// SGLang HTTP port.
    pub port: u16,
    /// Startup timeout in seconds.
    #[serde(rename = "startupSeconds")]
    pub startup_seconds: u64,
    /// Drain timeout in seconds.
    #[serde(rename = "drainSeconds")]
    pub drain_seconds: u64,
    /// Additional validated SGLang arguments.
    #[serde(rename = "extraArgs", default)]
    pub extra_args: Vec<String>,
    /// Group-local generate body limit.
    #[serde(
        rename = "internalGenerateRequestBodyLimitBytes",
        default = "default_body_limit"
    )]
    pub internal_generate_request_body_limit_bytes: usize,
}

impl SglangLaunchPlan {
    pub fn parse(input: &str) -> Result<Self, String> {
        let plan: Self = serde_json::from_str(input)
            .map_err(|error| format!("invalid FORETOKEN_SGLANG_LAUNCH_PLAN: {error}"))?;
        plan.validate()?;
        Ok(plan)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1 {
            return Err("launch plan version must be 1".into());
        }
        if self.model.is_empty() {
            return Err("launch plan model must be nonempty".into());
        }
        if self.tp == 0 || self.dp == 0 {
            return Err("launch plan tp and dp must be positive".into());
        }
        if self.port == 0 {
            return Err("launch plan port must be nonzero".into());
        }
        if self.startup_seconds == 0 || self.drain_seconds == 0 {
            return Err("launch plan lifecycle seconds must be positive".into());
        }
        validate_extra_args_shape(&self.extra_args)
    }

    /// Startup budget as a [`Duration`].
    pub fn startup_timeout(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.startup_seconds)
    }

    /// Drain budget as a [`Duration`].
    pub fn drain_timeout(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.drain_seconds)
    }

    /// Renders the SGLang launch command arguments.
    pub fn render_args(&self) -> Vec<String> {
        let mut args = vec![
            "python3".to_string(),
            "-m".to_string(),
            "sglang.launch_server".to_string(),
            format!("--model-path={}", self.model),
            format!("--host={LOOPBACK_HOST}"),
            format!("--port={}", self.port),
            format!("--tp-size={}", self.tp),
            format!("--dp-size={}", self.dp),
        ];
        if let Some(revision) = &self.revision {
            args.push(format!("--revision={revision}"));
        }
        args.extend(self.extra_args.iter().cloned());
        args
    }
}

/// Validates the shape of the controller-owned `extraArgs` payload.
fn validate_extra_args_shape(args: &[String]) -> Result<(), String> {
    let mut seen = HashSet::new();
    for argument in args {
        if argument.is_empty()
            || argument.contains(char::is_whitespace)
            || !argument.starts_with("--")
            || argument == "--"
        {
            return Err("extraArgs must be one nonempty --long-name token".into());
        }
        let name = argument
            .split_once('=')
            .map_or(argument.as_str(), |(name, _)| name);
        if argument.matches('=').count() > 1 || !seen.insert(name) {
            return Err(format!(
                "extraArgs flag {argument:?} is duplicated or malformed"
            ));
        }
    }
    Ok(())
}
