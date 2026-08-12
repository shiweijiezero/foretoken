// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Built-in Filter, Scorer, and Picker selection.

use std::str::FromStr;
use std::sync::Arc;

use serde::{Deserialize, Serialize};

use crate::algorithm::{
    AllowAllFilter, KvLeastLoadedScorer, LeastLoadedScorer, MaxPicker, RoundRobinPicker,
    UniformScorer,
};
use crate::{RoutePicker, RouteScorer, RouterPipeline};

macro_rules! algorithm_names {
    ($(#[$attribute:meta])* $type:ident { $($variant:ident => $value:literal),+ $(,)? } default $default:ident) => {
        $(#[$attribute])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
        #[serde(rename_all = "snake_case")]
        pub enum $type {
            $($variant),+
        }

        impl Default for $type {
            fn default() -> Self {
                Self::$default
            }
        }

        impl FromStr for $type {
            type Err = String;

            fn from_str(value: &str) -> Result<Self, Self::Err> {
                match value {
                    $($value => Ok(Self::$variant),)+
                    _ => Err(format!("unknown router algorithm {value:?}")),
                }
            }
        }
    };
}

algorithm_names!(
    /// Built-in Filter selection.
    FilterAlgorithm { AllowAll => "allow_all" } default AllowAll
);
algorithm_names!(
    /// Built-in Scorer selection.
    ScorerAlgorithm {
        Uniform => "uniform",
        LeastLoaded => "least_loaded",
        KvLeastLoaded => "kv_least_loaded",
    }
    default KvLeastLoaded
);
algorithm_names!(
    /// Built-in Picker selection.
    PickerAlgorithm {
        Max => "max",
        RoundRobin => "round_robin",
    }
    default RoundRobin
);

/// Built-in algorithms selected for each Router pipeline stage.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
pub struct RouterPipelineConfig {
    /// Filter used before scoring.
    pub filter: FilterAlgorithm,
    /// Scorer used to rank filtered candidates.
    pub scorer: ScorerAlgorithm,
    /// Picker used to select one scored candidate.
    pub picker: PickerAlgorithm,
}

impl RouterPipelineConfig {
    /// Builds the selected built-in Filter, Scorer, and Picker implementations.
    pub fn build(self) -> RouterPipeline {
        let scorer: Arc<dyn RouteScorer> = match self.scorer {
            ScorerAlgorithm::Uniform => Arc::new(UniformScorer),
            ScorerAlgorithm::LeastLoaded => Arc::new(LeastLoadedScorer),
            ScorerAlgorithm::KvLeastLoaded => Arc::new(KvLeastLoadedScorer),
        };
        let picker: Arc<dyn RoutePicker> = match self.picker {
            PickerAlgorithm::Max => Arc::new(MaxPicker),
            PickerAlgorithm::RoundRobin => Arc::new(RoundRobinPicker::default()),
        };
        RouterPipeline::new(Arc::new(AllowAllFilter), scorer, picker)
    }
}
