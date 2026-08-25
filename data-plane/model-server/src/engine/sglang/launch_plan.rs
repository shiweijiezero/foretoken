// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Private versioned launch contract for the SGLang adapter.
//!
//! SGLang has no Rust engine client; the adapter spawns
//! `python3 -m sglang.launch_server` on loopback and talks to its native HTTP
//! `/generate` endpoint. This module renders the typed plan into those argv.

use std::collections::HashSet;

use serde::Deserialize;

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
    /// Model repository or path served by SGLang.
    pub model: String,
    /// Optional model revision, forwarded to `--revision`.
    #[serde(default)]
    pub revision: Option<String>,
    /// Tensor-parallel size.
    #[serde(default = "default_tp")]
    pub tp: usize,
    /// Data-parallel size.
    #[serde(default = "default_dp")]
    pub dp: usize,
    /// GPU memory fraction, forwarded to `--mem-fraction-static`.
    #[serde(rename = "memFraction", default)]
    pub mem_fraction: Option<f64>,
    /// Loopback HTTP port for the SGLang server.
    pub port: u16,
    /// Startup budget in seconds.
    #[serde(rename = "startupSeconds")]
    pub startup_seconds: u64,
    /// Drain budget in seconds.
    #[serde(rename = "drainSeconds")]
    pub drain_seconds: u64,
    /// Additional `--long-name` arguments forwarded verbatim.
    #[serde(rename = "extraArgs", default)]
    pub extra_args: Vec<String>,
    /// Group-local generate request body limit.
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
        if let Some(fraction) = self.mem_fraction
            && !(0.0..=1.0).contains(&fraction)
        {
            return Err("launch plan memFraction must be within 0.0 and 1.0".into());
        }
        validate_extra_args(&self.extra_args)
    }

    /// Startup budget as a [`Duration`].
    pub fn startup_timeout(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.startup_seconds)
    }

    /// Drain budget as a [`Duration`].
    pub fn drain_timeout(&self) -> std::time::Duration {
        std::time::Duration::from_secs(self.drain_seconds)
    }

    /// Renders the SGLang launch-server argv, including the Python entrypoint.
    pub fn render_args(&self) -> Result<Vec<String>, String> {
        self.validate()?;
        let mut args = vec![
            "python3".to_string(),
            "-m".to_string(),
            "sglang.launch_server".to_string(),
            format!("--model-path={}", self.model),
            "--host=127.0.0.1".to_string(),
            format!("--port={}", self.port),
            format!("--tp-size={}", self.tp),
            format!("--dp-size={}", self.dp),
        ];
        if let Some(revision) = &self.revision {
            args.push(format!("--revision={revision}"));
        }
        if let Some(fraction) = self.mem_fraction {
            args.push(format!("--mem-fraction-static={fraction}"));
        }
        args.extend(self.extra_args.iter().cloned());
        Ok(args)
    }
}

/// Restricts `extraArgs` to a known allowlist of value/boolean flags.
fn validate_extra_args(args: &[String]) -> Result<(), String> {
    const VALUE_FLAGS: &[&str] = &[
        "--max-total-tokens",
        "--context-length",
        "--chunked-prefill-size",
        "--schedule-policy",
        "--attention-backend",
    ];
    const BOOL_FLAGS: &[&str] = &["--disable-radix-cache", "--enable-torch-compile"];
    let mut seen = HashSet::new();
    for argument in args {
        if argument.is_empty()
            || argument.contains(char::is_whitespace)
            || !argument.starts_with("--")
            || argument == "--"
        {
            return Err("extraArgs must be one nonempty --long-name token".into());
        }
        let (name, value) = argument
            .split_once('=')
            .map_or((argument.as_str(), None), |(name, value)| {
                (name, Some(value))
            });
        if argument.matches('=').count() > 1 || name.contains('_') || !seen.insert(name) {
            return Err(format!("extraArgs flag {name:?} is not allowed"));
        }
        if VALUE_FLAGS.contains(&name) {
            if value.is_none_or(str::is_empty) {
                return Err(format!("extraArgs flag {name:?} requires a value"));
            }
        } else if BOOL_FLAGS.contains(&name) {
            if value.is_some() {
                return Err(format!("extraArgs flag {name:?} does not take a value"));
            }
        } else {
            return Err(format!("extraArgs flag {name:?} is not allowed"));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plan() -> SglangLaunchPlan {
        SglangLaunchPlan::parse(
            r#"{"version":1,"model":"Qwen/Qwen3-0.6B","tp":1,"dp":1,"port":30000,"startupSeconds":120,"drainSeconds":30}"#,
        )
        .unwrap()
    }

    #[test]
    fn defaults_body_limit() {
        assert_eq!(
            plan().internal_generate_request_body_limit_bytes,
            64 * 1024 * 1024
        );
    }

    #[test]
    fn renders_launch_server_args() {
        let args = plan().render_args().unwrap();
        assert_eq!(args[0], "python3");
        assert!(args.contains(&"--model-path=Qwen/Qwen3-0.6B".to_string()));
        assert!(args.contains(&"--host=127.0.0.1".to_string()));
        assert!(args.contains(&"--port=30000".to_string()));
        assert!(args.contains(&"--tp-size=1".to_string()));
        assert!(args.contains(&"--dp-size=1".to_string()));
    }

    #[test]
    fn accepts_context_length() {
        let mut good = plan();
        good.extra_args = vec!["--context-length=8192".into()];
        assert!(good.validate().is_ok(), "--context-length was rejected");
        let args = good.render_args().unwrap();
        assert!(args.contains(&"--context-length=8192".to_string()));
    }

    #[test]
    fn rejects_invalid_extra_args() {
        for argument in [
            "--unknown=1",
            "--max-total-tokens",
            "--max_total_tokens=1",
            "--max-model-len=8192",
        ] {
            let mut bad = plan();
            bad.extra_args = vec![argument.into()];
            assert!(bad.validate().is_err(), "{argument} was accepted");
        }
    }

    #[test]
    fn rejects_malformed_fields() {
        let mut bad = plan();
        bad.tp = 0;
        assert!(bad.validate().is_err());

        let mut bad = plan();
        bad.mem_fraction = Some(1.5);
        assert!(bad.validate().is_err());
    }
}
