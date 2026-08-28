// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Configured Filter, Scorer, and Picker selection.

use std::fmt;
use std::str::FromStr;
use std::sync::Arc;

use serde::{Deserialize, Deserializer, Serialize, Serializer};
use thiserror::Error;

use crate::{RouteFilter, RoutePicker, RouteScorer, RouterPipeline};

/// A Filter implementation compiled into this binary.
pub struct FilterDescriptor {
    /// Stable lower-snake-case configuration name.
    pub name: &'static str,
    /// Constructs the implementation selected by `name`.
    pub factory: fn() -> Arc<dyn RouteFilter>,
}
inventory::collect!(FilterDescriptor);

/// A Scorer implementation compiled into this binary.
pub struct ScorerDescriptor {
    /// Stable lower-snake-case configuration name.
    pub name: &'static str,
    /// Constructs the implementation selected by `name`.
    pub factory: fn() -> Arc<dyn RouteScorer>,
}
inventory::collect!(ScorerDescriptor);

/// A Picker implementation compiled into this binary.
pub struct PickerDescriptor {
    /// Stable lower-snake-case configuration name.
    pub name: &'static str,
    /// Constructs the implementation selected by `name`.
    pub factory: fn() -> Arc<dyn RoutePicker>,
}
inventory::collect!(PickerDescriptor);

/// One lower-snake-case algorithm name selected in the pipeline configuration.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct AlgorithmName(String);

impl AlgorithmName {
    fn new(value: String) -> Result<Self, RouterPipelineConfigError> {
        if value.is_empty() {
            return Err(RouterPipelineConfigError::EmptyName);
        }
        if !is_lower_snake_case(&value) {
            return Err(RouterPipelineConfigError::InvalidName { name: value });
        }
        Ok(Self(value))
    }

    /// Returns the configured stable algorithm name.
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl fmt::Display for AlgorithmName {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        self.0.fmt(formatter)
    }
}

impl FromStr for AlgorithmName {
    type Err = RouterPipelineConfigError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        Self::new(value.to_owned())
    }
}

impl Serialize for AlgorithmName {
    fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
    where
        S: Serializer,
    {
        serializer.serialize_str(&self.0)
    }
}

impl<'de> Deserialize<'de> for AlgorithmName {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: Deserializer<'de>,
    {
        String::deserialize(deserializer)?
            .parse()
            .map_err(serde::de::Error::custom)
    }
}

/// Configured Filter selection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct FilterAlgorithm(AlgorithmName);

/// Configured Scorer selection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct ScorerAlgorithm(AlgorithmName);

/// Configured Picker selection.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(transparent)]
pub struct PickerAlgorithm(AlgorithmName);

macro_rules! algorithm_name_wrapper {
    ($type:ident, $default:literal) => {
        impl $type {
            /// Returns this configuration's stable lower-snake-case name.
            pub fn as_str(&self) -> &str {
                self.0.as_str()
            }
        }

        impl Default for $type {
            fn default() -> Self {
                $default.parse().expect("built-in algorithm name is valid")
            }
        }

        impl FromStr for $type {
            type Err = RouterPipelineConfigError;

            fn from_str(value: &str) -> Result<Self, Self::Err> {
                Ok(Self(value.parse()?))
            }
        }

        impl fmt::Display for $type {
            fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                self.0.fmt(formatter)
            }
        }
    };
}

algorithm_name_wrapper!(FilterAlgorithm, "allow_all");
algorithm_name_wrapper!(ScorerAlgorithm, "kv_least_loaded");
algorithm_name_wrapper!(PickerAlgorithm, "round_robin");

/// Configured algorithms selected for each Router pipeline stage.
#[derive(Debug, Clone, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouterPipelineConfig {
    /// Filter used before scoring.
    #[serde(default)]
    pub filter: FilterAlgorithm,
    /// Scorer used to rank filtered candidates.
    #[serde(default)]
    pub scorer: ScorerAlgorithm,
    /// Picker used to select one scored candidate.
    #[serde(default)]
    pub picker: PickerAlgorithm,
}

impl RouterPipelineConfig {
    /// Builds the selected Filter, Scorer, and Picker implementations compiled into this binary.
    pub fn build(&self) -> Result<RouterPipeline, RouterPipelineConfigError> {
        validate_descriptors()?;
        Ok(RouterPipeline::new(
            filter_factory(self.filter.as_str())?(),
            scorer_factory(self.scorer.as_str())?(),
            picker_factory(self.picker.as_str())?(),
        ))
    }

    /// Validates all compiled descriptors and configured names before serving begins.
    pub fn validate(&self) -> Result<(), RouterPipelineConfigError> {
        self.build().map(|_| ())
    }
}

fn filter_factory(name: &str) -> Result<fn() -> Arc<dyn RouteFilter>, RouterPipelineConfigError> {
    inventory::iter::<FilterDescriptor>
        .into_iter()
        .find(|descriptor| descriptor.name == name)
        .map(|descriptor| descriptor.factory)
        .ok_or_else(|| RouterPipelineConfigError::UnknownAlgorithm {
            category: "filter",
            name: name.to_owned(),
        })
}

fn scorer_factory(name: &str) -> Result<fn() -> Arc<dyn RouteScorer>, RouterPipelineConfigError> {
    inventory::iter::<ScorerDescriptor>
        .into_iter()
        .find(|descriptor| descriptor.name == name)
        .map(|descriptor| descriptor.factory)
        .ok_or_else(|| RouterPipelineConfigError::UnknownAlgorithm {
            category: "scorer",
            name: name.to_owned(),
        })
}

fn picker_factory(name: &str) -> Result<fn() -> Arc<dyn RoutePicker>, RouterPipelineConfigError> {
    inventory::iter::<PickerDescriptor>
        .into_iter()
        .find(|descriptor| descriptor.name == name)
        .map(|descriptor| descriptor.factory)
        .ok_or_else(|| RouterPipelineConfigError::UnknownAlgorithm {
            category: "picker",
            name: name.to_owned(),
        })
}

fn validate_descriptors() -> Result<(), RouterPipelineConfigError> {
    validate_descriptor_names(
        "filter",
        inventory::iter::<FilterDescriptor>
            .into_iter()
            .map(|descriptor| descriptor.name),
    )?;
    validate_descriptor_names(
        "scorer",
        inventory::iter::<ScorerDescriptor>
            .into_iter()
            .map(|descriptor| descriptor.name),
    )?;
    validate_descriptor_names(
        "picker",
        inventory::iter::<PickerDescriptor>
            .into_iter()
            .map(|descriptor| descriptor.name),
    )
}

fn validate_descriptor_names<'a>(
    category: &'static str,
    names: impl IntoIterator<Item = &'a str>,
) -> Result<(), RouterPipelineConfigError> {
    let mut names = names.into_iter().collect::<Vec<_>>();
    names.sort_unstable();
    for name in &names {
        if name.is_empty() {
            return Err(RouterPipelineConfigError::EmptyDescriptorName { category });
        }
        if !is_lower_snake_case(name) {
            return Err(RouterPipelineConfigError::InvalidDescriptorName {
                category,
                name: (*name).to_owned(),
            });
        }
    }
    if let Some(name) = names
        .windows(2)
        .find_map(|names| (names[0] == names[1]).then_some(names[0]))
    {
        return Err(RouterPipelineConfigError::DuplicateDescriptorName {
            category,
            name: name.to_owned(),
        });
    }
    Ok(())
}

fn is_lower_snake_case(value: &str) -> bool {
    !value.starts_with('_')
        && !value.ends_with('_')
        && !value.contains("__")
        && value
            .bytes()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || byte == b'_')
}

/// A pipeline configuration or compiled registry is invalid.
#[derive(Debug, Error, PartialEq, Eq)]
pub enum RouterPipelineConfigError {
    /// A configured name was empty.
    #[error("router algorithm name must not be empty")]
    EmptyName,
    /// A configured name is not lower snake case.
    #[error("router algorithm name {name:?} must be lower snake case")]
    InvalidName { name: String },
    /// A compiled implementation omitted its name.
    #[error("compiled {category} descriptor has an empty name")]
    EmptyDescriptorName { category: &'static str },
    /// A compiled implementation used an invalid name.
    #[error("compiled {category} descriptor name {name:?} must be lower snake case")]
    InvalidDescriptorName {
        category: &'static str,
        name: String,
    },
    /// Two compiled implementations claim the same name.
    #[error("duplicate compiled {category} algorithm name {name:?}")]
    DuplicateDescriptorName {
        category: &'static str,
        name: String,
    },
    /// The configuration selected no compiled implementation.
    #[error("unknown compiled {category} algorithm {name:?}")]
    UnknownAlgorithm {
        category: &'static str,
        name: String,
    },
}
