// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Router pipeline registry and configuration tests.

use foretoken_router::{
    FilterAlgorithm, FilterStage, PickerStage, RouterPipelineConfig, RouterPipelineConfigError,
    ScorerStage,
};

// Protects every documented built-in router algorithm from missing compile-time registration.
#[test]
fn every_compiled_builtin_name_parses_and_builds() {
    for (filter, scorer, picker) in [
        ("allow_all", "uniform", "max"),
        ("allow_all", "kv_least_loaded", "gamble_sampling"),
        ("allow_all", "kv_least_loaded", "power_of_two_choices"),
        ("allow_all", "least_loaded", "max"),
        ("allow_all", "running_request", "max"),
        ("allow_all", "kv_cache_utilization", "max"),
        ("allow_all", "queue_depth", "max"),
        ("allow_all", "token_load", "max"),
        ("allow_all", "prefix", "max"),
    ] {
        let config = RouterPipelineConfig {
            filter: FilterStage {
                algorithm: filter.parse().unwrap(),
                parameters: Default::default(),
            },
            scorer: ScorerStage {
                algorithm: scorer.parse().unwrap(),
                parameters: Default::default(),
            },
            picker: PickerStage {
                algorithm: picker.parse().unwrap(),
                parameters: Default::default(),
            },
        };
        let _ = config.build().unwrap();
    }
}

// Protects user configuration from empty or unavailable algorithm names while allowing opaque names.
#[test]
fn empty_and_unknown_names_are_explicit_errors() {
    assert_eq!(
        "".parse::<FilterAlgorithm>(),
        Err(RouterPipelineConfigError::EmptyName)
    );
    let unknown = RouterPipelineConfig {
        filter: FilterStage {
            algorithm: "allow_all".parse().unwrap(),
            parameters: Default::default(),
        },
        scorer: ScorerStage {
            algorithm: "community-scorer".parse().unwrap(),
            parameters: Default::default(),
        },
        picker: PickerStage {
            algorithm: "gamble_sampling".parse().unwrap(),
            parameters: Default::default(),
        },
    };
    assert!(matches!(
        unknown.build(),
        Err(RouterPipelineConfigError::UnknownAlgorithm {
            category: "scorer",
            name,
        }) if name == "community-scorer"
    ));
}
