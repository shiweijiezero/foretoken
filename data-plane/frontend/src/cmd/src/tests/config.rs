// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::collections::BTreeMap;

use foretoken_router::RouterPipelineConfig;

use crate::config::router_pipeline_from_env;

#[test]
fn router_environment_builds_every_compiled_builtin_pipeline() {
    for (filter, scorer, picker) in [
        ("allow_all", "uniform", "max"),
        ("allow_all", "least_loaded", "round_robin"),
        ("allow_all", "kv_least_loaded", "round_robin"),
    ] {
        let values = BTreeMap::from([
            ("FORETOKEN_ROUTER_FILTER", filter),
            ("FORETOKEN_ROUTER_SCORER", scorer),
            ("FORETOKEN_ROUTER_PICKER", picker),
        ]);
        let parsed = router_pipeline_from_env(|name| {
            values
                .get(name)
                .map(|value| (*value).to_owned())
                .ok_or(std::env::VarError::NotPresent)
        })
        .unwrap();
        assert_eq!(parsed.filter.as_str(), filter);
        assert_eq!(parsed.scorer.as_str(), scorer);
        assert_eq!(parsed.picker.as_str(), picker);
        let _ = parsed.build().unwrap();
    }
}

#[test]
fn router_environment_rejects_unknown_or_empty_algorithms() {
    for (name, value, expected) in [
        (
            "FORETOKEN_ROUTER_FILTER",
            "",
            "router algorithm name must not be empty",
        ),
        (
            "FORETOKEN_ROUTER_SCORER",
            "not_compiled",
            "unknown compiled scorer algorithm \"not_compiled\"",
        ),
    ] {
        let values = BTreeMap::from([(name, value)]);
        let error = router_pipeline_from_env(|key| {
            values
                .get(key)
                .map(|value| (*value).to_owned())
                .ok_or(std::env::VarError::NotPresent)
        })
        .unwrap_err();
        assert_eq!(error, expected);
    }

    assert_eq!(
        router_pipeline_from_env(|_| Err(std::env::VarError::NotPresent)).unwrap(),
        RouterPipelineConfig::default()
    );
}
