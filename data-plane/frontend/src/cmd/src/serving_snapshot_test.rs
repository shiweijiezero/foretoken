// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::Arc;

use async_trait::async_trait;
use foretoken_backend_registry::BackendRegistry;
use foretoken_router::{PipelineRouter, RouterPipelineConfig};
use foretoken_runtime_builder::{KvIndexCredential, RuntimeBuilder};
use foretoken_server::{Generation, RuntimeControl, RuntimeGeneration, RuntimeState};

use super::process_serving_snapshot;

fn runtime_builder() -> RuntimeBuilder {
    RuntimeBuilder::new(RouterPipelineConfig::default(), KvIndexCredential::Disabled)
}

struct ReadyControl;

#[async_trait]
impl RuntimeControl for ReadyControl {
    async fn refresh_backend_readiness(&self) {}

    fn configured_models(&self) -> Vec<String> {
        vec!["active".into()]
    }

    fn is_ready(&self) -> bool {
        true
    }
}

fn active_generation(version: u64) -> Arc<RuntimeGeneration> {
    let registry = Arc::new(
        BackendRegistry::from_json(
            br#"{"version":1,"groups":[{"service_uid":"service","pool_uid":"pool","pool_name":"pool","route_target_id":"active","model":"active","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1","data_parallel_size":1}]}"#,
        )
        .unwrap(),
    );
    let state = Arc::new(RuntimeState::new(
        Default::default(),
        Arc::new(PipelineRouter::new(registry.clone())),
        registry,
    ));
    let generation = Arc::new(RuntimeGeneration::new());
    assert!(generation.replace_state(version, state, Arc::new(ReadyControl)));
    generation
}

#[tokio::test]
async fn invalid_unready_or_stale_candidate_never_revokes_the_active_generation() {
    let generation = active_generation(10);
    let builder = runtime_builder();

    assert!(!process_serving_snapshot(&generation, b"not-json", &builder).await);

    let unready = br#"{"version":11,"groups":[{"route_target_id":"candidate","model":"candidate","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1","data_parallel_size":1}]}"#;
    assert!(!process_serving_snapshot(&generation, unready, &builder).await);

    let stale = br#"{"version":9,"groups":[{"route_target_id":"stale","model":"stale","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://127.0.0.1:1","data_parallel_size":1}]}"#;
    assert!(process_serving_snapshot(&generation, stale, &builder).await);

    assert!(generation.ready());
    assert_eq!(generation.configured_models(), vec!["active"]);
    assert_eq!(generation.active_version(), Some(10));
}
