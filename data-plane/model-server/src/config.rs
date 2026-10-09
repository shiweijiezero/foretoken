// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Environment boundary for the controller-owned typed launch plan.

use std::net::SocketAddr;

use crate::launch::LaunchPlanV1;

const LAUNCH_PLAN_ENV: &str = "FORETOKEN_VLLM_LAUNCH_PLAN";
const LISTEN_ENV: &str = "FORETOKEN_INTERNAL_LISTEN";
/// Controller-projected ModelGroup identity shared by all member Pods.
pub const MODEL_GROUP_UID_ENV: &str = "FORETOKEN_MODEL_GROUP_UID";

#[derive(Debug, Clone, PartialEq)]
pub struct RuntimeConfig {
    pub launch: LaunchPlanV1,
    pub listen_address: SocketAddr,
    pub member: Option<MemberContext>,
    pub max_accepted_requests: Option<u32>,
}

/// Pod-local identity supplied by Kubernetes and LeaderWorkerSet for distributed startup.
#[derive(Debug, Clone, PartialEq)]
pub struct MemberContext {
    pub model_group_uid: String,
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
                model_group_uid: required_env(MODEL_GROUP_UID_ENV)?,
                index,
                address,
                leader_address: required_env("LWS_LEADER_ADDRESS")?,
            })
        } else {
            None
        };
        let max_accepted_requests = match std::env::var("FORETOKEN_MAX_ACCEPTED_REQUESTS") {
            Ok(value) => Some(
                value
                    .parse::<u32>()
                    .ok()
                    .filter(|limit| *limit > 0)
                    .ok_or("FORETOKEN_MAX_ACCEPTED_REQUESTS must be a positive integer")?,
            ),
            Err(std::env::VarError::NotPresent) => None,
            Err(_) => return Err("FORETOKEN_MAX_ACCEPTED_REQUESTS must be valid Unicode".into()),
        };
        Ok(Self {
            launch,
            listen_address,
            member,
            max_accepted_requests,
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
