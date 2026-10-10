// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolved model waiting limits and caller rules published by the control plane.

use std::collections::BTreeSet;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Effective settings for one public model within a frontend service.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct AdmissionConfig {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_waiting_requests: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub queue_timeout: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub role_rules: Vec<RoleRule>,
}

/// Scheduling and capacity assigned to each caller with the matching trusted role.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct RoleRule {
    pub role: String,
    #[serde(default)]
    pub priority: i32,
    pub per_caller: CallerCapacity,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub allowed_pools: Vec<String>,
}

/// Independent waiting and unfinished-generation limits for each caller.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct CallerCapacity {
    pub max_waiting_requests: u32,
    pub max_concurrent_requests: u32,
}

impl AdmissionConfig {
    /// Validates a candidate without modifying the live queue or reservation ledger.
    pub(crate) fn validate(&self) -> Result<(), AdmissionConfigError> {
        if self.max_waiting_requests == Some(0) {
            return Err(AdmissionConfigError(
                "maxWaitingRequests must be positive".into(),
            ));
        }
        if !self.role_rules.is_empty() && self.max_waiting_requests.is_none() {
            return Err(AdmissionConfigError(
                "roleRules requires admission.maxWaitingRequests".into(),
            ));
        }
        self.wait_timeout()?;
        let mut roles = BTreeSet::new();
        for rule in &self.role_rules {
            if rule.role.is_empty() || !roles.insert(&rule.role) {
                return Err(AdmissionConfigError(
                    "roleRules requires unique nonempty roles".into(),
                ));
            }
            if rule.per_caller.max_waiting_requests == 0
                || rule.per_caller.max_concurrent_requests == 0
            {
                return Err(AdmissionConfigError(format!(
                    "role {:?} caller limits must be positive",
                    rule.role
                )));
            }
        }
        Ok(())
    }

    /// Resolves the optional waiting budget for request admission and configuration validation.
    pub(crate) fn wait_timeout(&self) -> Result<Option<Duration>, AdmissionConfigError> {
        self.queue_timeout
            .as_ref()
            .map(|value| {
                humantime::parse_duration(value)
                    .map_err(|error| AdmissionConfigError(format!("queueTimeout: {error}")))
                    .and_then(|duration| {
                        if duration.is_zero() {
                            Err(AdmissionConfigError("queueTimeout must be positive".into()))
                        } else {
                            Ok(duration)
                        }
                    })
            })
            .transpose()
    }
}

/// Invalid resolved settings; the active publication remains in effect.
#[derive(Debug, Error)]
#[error("invalid admission configuration: {0}")]
pub struct AdmissionConfigError(pub String);
