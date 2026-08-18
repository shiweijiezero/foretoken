// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Environment boundary for the controller-owned typed launch plan.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::time::Duration;

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
        let mut values = HashMap::new();
        for name in [LAUNCH_PLAN_ENV, LISTEN_ENV] {
            let value = std::env::var(name).map_err(|error| match error {
                std::env::VarError::NotPresent => {
                    format!("{name} must be set by the ModelGroup controller")
                }
                std::env::VarError::NotUnicode(_) => format!("{name} must be valid Unicode"),
            })?;
            values.insert(name.to_string(), value);
        }
        Self::from_values(&values)
    }

    pub(crate) fn from_values(values: &HashMap<String, String>) -> Result<Self, String> {
        let launch = LaunchPlanV1::parse(&required(values, LAUNCH_PLAN_ENV)?)?;
        let listen_address = required(values, LISTEN_ENV)?
            .parse()
            .map_err(|_| format!("{LISTEN_ENV} must be a socket address"))?;
        Ok(Self {
            launch,
            listen_address,
        })
    }

    pub fn startup_timeout(&self) -> Duration {
        self.launch.startup_timeout()
    }
    pub fn drain_timeout(&self) -> Duration {
        self.launch.drain_timeout()
    }
}

fn required(values: &HashMap<String, String>, name: &str) -> Result<String, String> {
    values
        .get(name)
        .filter(|value| !value.is_empty())
        .cloned()
        .ok_or_else(|| format!("{name} must be set by the ModelGroup controller"))
}
