// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Built-in routing algorithms and their stable user-facing identifiers.

use std::str::FromStr;
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::builtins::{
    AllowAllFilter, KvLoadScorer, LeastLoadedScorer, StablePicker, TopologyScorer,
};
use crate::{KvPrefixScorer, PolicyRouter, RouteInventory, RouteScorer, RouterPolicy};

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum RouterAlgorithm {
    #[default]
    KvAware,
    LeastLoaded,
    RoundRobin,
}

impl RouterAlgorithm {
    pub const ALL: [Self; 3] = [Self::KvAware, Self::LeastLoaded, Self::RoundRobin];

    pub const fn as_str(self) -> &'static str {
        match self {
            Self::KvAware => "kv_aware",
            Self::LeastLoaded => "least_loaded",
            Self::RoundRobin => "round_robin",
        }
    }

    pub fn policy(self, prefix: Arc<dyn KvPrefixScorer>) -> RouterPolicy {
        let scorer: Arc<dyn RouteScorer> = match self {
            Self::KvAware => Arc::new(KvLoadScorer::new(prefix)),
            Self::LeastLoaded => Arc::new(LeastLoadedScorer),
            Self::RoundRobin => Arc::new(TopologyScorer),
        };
        RouterPolicy::new(Arc::new(AllowAllFilter), scorer, Arc::new(StablePicker))
    }
}

impl FromStr for RouterAlgorithm {
    type Err = String;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "kv_aware" => Ok(Self::KvAware),
            "least_loaded" => Ok(Self::LeastLoaded),
            "round_robin" => Ok(Self::RoundRobin),
            _ => Err(format!(
                "router algorithm must be one of: {}",
                Self::ALL
                    .iter()
                    .map(|algorithm| algorithm.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            )),
        }
    }
}

impl PolicyRouter {
    pub fn new(inventory: Arc<dyn RouteInventory>, prefix: Arc<dyn KvPrefixScorer>) -> Self {
        Self::with_algorithm(inventory, prefix, RouterAlgorithm::default())
    }

    pub fn with_algorithm(
        inventory: Arc<dyn RouteInventory>,
        prefix: Arc<dyn KvPrefixScorer>,
        algorithm: RouterAlgorithm,
    ) -> Self {
        Self::with_policy(inventory, algorithm.policy(prefix))
    }
}
