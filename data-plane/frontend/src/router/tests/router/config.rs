// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Router pipeline registry and configuration tests.

use foretoken_router::{
    FilterAlgorithm, PickerAlgorithm, RouterPipelineConfig, RouterPipelineConfigError,
    ScorerAlgorithm,
};

// Protects every documented built-in router algorithm from missing compile-time registration.
#[test]
fn every_compiled_builtin_name_parses_and_builds() {
    for (filter, scorer, picker) in [
        ("allow_all", "uniform", "max"),
        ("allow_all", "least_loaded", "round_robin"),
        ("allow_all", "kv_least_loaded", "round_robin"),
    ] {
        let config = RouterPipelineConfig {
            filter: filter.parse().unwrap(),
            scorer: scorer.parse().unwrap(),
            picker: picker.parse().unwrap(),
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
    assert!("community-scorer".parse::<ScorerAlgorithm>().is_ok());
    let unknown = RouterPipelineConfig {
        filter: "allow_all".parse().unwrap(),
        scorer: "community-scorer".parse().unwrap(),
        picker: PickerAlgorithm::default(),
    };
    assert!(matches!(
        unknown.build(),
        Err(RouterPipelineConfigError::UnknownAlgorithm {
            category: "scorer",
            name,
        }) if name == "community-scorer"
    ));
}
