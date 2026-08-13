// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Router pipeline registry and configuration tests.

use foretoken_router::{
    FilterAlgorithm, PickerAlgorithm, RouterPipelineConfig, RouterPipelineConfigError,
    ScorerAlgorithm,
};

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
        config.validate().unwrap();
        let _ = config.build().unwrap();
    }
}

#[test]
fn defaults_serialize_as_stable_snake_case_names() {
    let config = RouterPipelineConfig::default();
    assert_eq!(config.filter.as_str(), "allow_all");
    assert_eq!(config.scorer.as_str(), "kv_least_loaded");
    assert_eq!(config.picker.as_str(), "round_robin");
    assert_eq!(
        serde_json::to_string(&config).unwrap(),
        r#"{"filter":"allow_all","scorer":"kv_least_loaded","picker":"round_robin"}"#
    );
}

#[test]
fn invalid_unknown_and_duplicate_names_are_explicit_errors() {
    assert_eq!(
        "".parse::<FilterAlgorithm>(),
        Err(RouterPipelineConfigError::EmptyName)
    );
    assert!(matches!(
        "not-snake-case".parse::<ScorerAlgorithm>(),
        Err(RouterPipelineConfigError::InvalidName { .. })
    ));
    let unknown = RouterPipelineConfig {
        filter: "allow_all".parse().unwrap(),
        scorer: "community_scorer".parse().unwrap(),
        picker: PickerAlgorithm::default(),
    };
    assert!(matches!(
        unknown.build(),
        Err(RouterPipelineConfigError::UnknownAlgorithm {
            category: "scorer",
            name,
        }) if name == "community_scorer"
    ));
    assert_eq!(
        foretoken_router::validate_descriptor_names("picker", ["round_robin", "max", "max"]),
        Err(RouterPipelineConfigError::DuplicateDescriptorName {
            category: "picker",
            name: "max".into(),
        })
    );
}
