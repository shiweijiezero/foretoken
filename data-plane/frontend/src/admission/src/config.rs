// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Configuration and compiled algorithm registration for frontend admission.

use std::collections::HashSet;
use std::sync::Arc;

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::telemetry::AdmissionMetricsScope;
use crate::{Admission, AdmissionRule};

/// A compiled admission rule that accepts its own configuration parameters.
pub struct AdmissionDescriptor {
    /// Stable name selected by frontend configuration.
    pub name: &'static str,
    /// Constructs the rule once before the frontend starts accepting requests.
    pub factory: fn(serde_json::Value) -> Result<Arc<dyn AdmissionRule>, String>,
}
inventory::collect!(AdmissionDescriptor);

/// Process-local admission selection, independent of the routing pipeline.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default, deny_unknown_fields)]
pub struct AdmissionConfig {
    /// Compiled rule selected for intake and work admission.
    pub algorithm: String,
    /// Parameters interpreted by the selected rule at startup.
    #[serde(skip_serializing_if = "serde_json::Map::is_empty")]
    pub parameters: serde_json::Map<String, serde_json::Value>,
}

impl Default for AdmissionConfig {
    fn default() -> Self {
        Self {
            algorithm: "allow_all".into(),
            parameters: Default::default(),
        }
    }
}

impl AdmissionConfig {
    /// Constructs the configured rule and metric owner for the frontend's lifetime.
    pub fn build(&self) -> Result<Admission, AdmissionConfigError> {
        let mut names = HashSet::new();
        let mut selected = None;
        for descriptor in inventory::iter::<AdmissionDescriptor> {
            if descriptor.name.is_empty() {
                return Err(AdmissionConfigError::EmptyDescriptorName);
            }
            if !names.insert(descriptor.name) {
                return Err(AdmissionConfigError::DuplicateDescriptorName(
                    descriptor.name,
                ));
            }
            if descriptor.name == self.algorithm {
                selected = Some(descriptor);
            }
        }
        let descriptor = selected
            .ok_or_else(|| AdmissionConfigError::UnknownAlgorithm(self.algorithm.clone()))?;
        let rule = (descriptor.factory)(serde_json::Value::Object(self.parameters.clone()))
            .map_err(|message| AdmissionConfigError::InvalidParameters {
                algorithm: self.algorithm.clone(),
                message,
            })?;
        let metrics = AdmissionMetricsScope::new(descriptor.name, rule.capacity());
        Ok(Admission {
            rule,
            _metrics: metrics,
        })
    }
}

/// Invalid admission configuration or ambiguous compiled rule registration.
#[derive(Debug, Error)]
pub enum AdmissionConfigError {
    #[error("unknown admission algorithm {0:?}")]
    UnknownAlgorithm(String),
    #[error("invalid parameters for admission algorithm {algorithm:?}: {message}")]
    InvalidParameters { algorithm: String, message: String },
    #[error("compiled admission descriptor has an empty name")]
    EmptyDescriptorName,
    #[error("duplicate compiled admission algorithm name {0:?}")]
    DuplicateDescriptorName(&'static str),
}
