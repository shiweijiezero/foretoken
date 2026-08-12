// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Router pipeline configuration tests.

use foretoken_router::{FilterAlgorithm, PickerAlgorithm, RouterPipelineConfig, ScorerAlgorithm};

#[test]
fn algorithm_names_and_defaults_match_control_plane_values() {
    assert_eq!("allow_all".parse(), Ok(FilterAlgorithm::AllowAll));
    assert_eq!("uniform".parse(), Ok(ScorerAlgorithm::Uniform));
    assert_eq!("least_loaded".parse(), Ok(ScorerAlgorithm::LeastLoaded));
    assert_eq!(
        "kv_least_loaded".parse(),
        Ok(ScorerAlgorithm::KvLeastLoaded)
    );
    assert_eq!("max".parse(), Ok(PickerAlgorithm::Max));
    assert_eq!("round_robin".parse(), Ok(PickerAlgorithm::RoundRobin));
    assert_eq!(
        RouterPipelineConfig::default(),
        RouterPipelineConfig {
            filter: FilterAlgorithm::AllowAll,
            scorer: ScorerAlgorithm::KvLeastLoaded,
            picker: PickerAlgorithm::RoundRobin,
        }
    );
}
