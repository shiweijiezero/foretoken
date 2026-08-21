// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Environment boundary for the controller-owned typed launch plan.

use std::net::SocketAddr;

use crate::launch::LaunchPlanV1;

const LAUNCH_PLAN_ENV: &str = "FORETOKEN_VLLM_LAUNCH_PLAN";
const LISTEN_ENV: &str = "FORETOKEN_INTERNAL_LISTEN";

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RuntimeConfig {
    pub launch: LaunchPlanV1,
    pub listen_address: SocketAddr,
}

impl RuntimeConfig {
    pub fn from_env() -> Result<Self, String> {
        let launch = LaunchPlanV1::parse(&required_env(LAUNCH_PLAN_ENV)?)?;
        let listen_address = required_env(LISTEN_ENV)?
            .parse()
            .map_err(|_| format!("{LISTEN_ENV} must be a socket address"))?;
        Ok(Self {
            launch,
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
