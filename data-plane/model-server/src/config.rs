// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Environment boundary for the controller-owned typed launch plan.

use std::net::SocketAddr;

use crate::launch::LaunchPlanV1;

const LAUNCH_PLAN_ENV: &str = "FORETOKEN_VLLM_LAUNCH_PLAN";
const LISTEN_ENV: &str = "FORETOKEN_INTERNAL_LISTEN";

#[derive(Debug, Clone, PartialEq)]
pub struct RuntimeConfig {
    pub launch: LaunchPlanV1,
    pub listen_address: SocketAddr,
    pub member: Option<MemberContext>,
}

/// Pod-local identity supplied by Kubernetes and LeaderWorkerSet for distributed startup.
#[derive(Debug, Clone, PartialEq)]
pub struct MemberContext {
    pub index: usize,
    pub address: std::net::IpAddr,
    pub leader_address: String,
}

impl MemberContext {
    /// Resolves a member through LWS's leader/worker DNS naming within the same group.
    pub fn node_address(&self, index: usize) -> String {
        if index == 0 {
            return self.leader_address.clone();
        }
        match self.leader_address.split_once('.') {
            Some((leader, domain)) => format!("{leader}-{index}.{domain}"),
            None => format!("{}-{index}", self.leader_address),
        }
    }
}

impl RuntimeConfig {
    /// Reads the controller-projected launch and listen settings for model-server bootstrap.
    ///
    /// Startup receives an owned configuration; environment values are not retained after parsing.
    pub fn from_env() -> Result<Self, String> {
        let launch = LaunchPlanV1::parse(&required_env(LAUNCH_PLAN_ENV)?)?;
        let listen_address = required_env(LISTEN_ENV)?
            .parse()
            .map_err(|_| format!("{LISTEN_ENV} must be a socket address"))?;
        let member = if launch.node_count > 1 {
            let index = required_env("LWS_WORKER_INDEX")?
                .parse::<usize>()
                .map_err(|_| "LWS_WORKER_INDEX must be a nonnegative integer".to_string())?;
            if index >= launch.node_count {
                return Err("LWS_WORKER_INDEX is outside the model group".into());
            }
            let address = required_env("FORETOKEN_MEMBER_IP")?
                .parse::<std::net::IpAddr>()
                .map_err(|_| "FORETOKEN_MEMBER_IP must be a Pod IP address".to_string())?;
            Some(MemberContext {
                index,
                address,
                leader_address: required_env("LWS_LEADER_ADDRESS")?,
            })
        } else {
            None
        };
        Ok(Self {
            launch,
            listen_address,
            member,
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
