// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Environment boundary for the controller-owned typed launch plan.
//!
//! The launch plan is read as a raw payload; the engine adapter selected at
//! build time (via the backend feature) parses it. Each backend reads its own
//! controller-injected environment variable, so the binary never decides its
//! engine at runtime.

use std::net::SocketAddr;

const LISTEN_ENV: &str = "FORETOKEN_INTERNAL_LISTEN";
#[cfg(feature = "backend-vllm")]
const LAUNCH_PLAN_ENV: &str = "FORETOKEN_VLLM_LAUNCH_PLAN";
#[cfg(all(feature = "backend-sglang", not(feature = "backend-vllm")))]
const LAUNCH_PLAN_ENV: &str = "FORETOKEN_SGLANG_LAUNCH_PLAN";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeConfig {
    pub launch_payload: String,
    pub listen_address: SocketAddr,
}

impl RuntimeConfig {
    pub fn from_env() -> Result<Self, String> {
        let launch_payload = required_env(LAUNCH_PLAN_ENV)?;
        let listen_address = required_env(LISTEN_ENV)?
            .parse()
            .map_err(|_| format!("{LISTEN_ENV} must be a socket address"))?;
        Ok(Self {
            launch_payload,
            listen_address,
        })
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
    fn launch_plan_env_matches_backend_feature() {
        #[cfg(feature = "backend-vllm")]
        assert_eq!(LAUNCH_PLAN_ENV, "FORETOKEN_VLLM_LAUNCH_PLAN");
        #[cfg(all(feature = "backend-sglang", not(feature = "backend-vllm")))]
        assert_eq!(LAUNCH_PLAN_ENV, "FORETOKEN_SGLANG_LAUNCH_PLAN");
    }

    #[test]
    fn listen_address_parses() {
        let addr: SocketAddr = "0.0.0.0:9000".parse().expect("valid socket address");
        assert_eq!(addr.port(), 9000);
    }
}
