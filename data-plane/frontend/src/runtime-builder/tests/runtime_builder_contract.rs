// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use foretoken_backend_registry::{ServingSnapshot, SnapshotModel};
use foretoken_router::{RouterPipelineConfig, ScalingTarget, ScalingTargetKind};
use foretoken_runtime_builder::{KvIndexCredential, RuntimeBuildError, RuntimeBuilder};
use foretoken_server::{Generation, RuntimeGeneration};

fn builder() -> RuntimeBuilder {
    RuntimeBuilder::new(RouterPipelineConfig::default(), KvIndexCredential::Disabled)
}

#[tokio::test]
async fn logical_only_snapshot_publishes_a_ready_scale_from_zero_runtime() {
    let snapshot = ServingSnapshot {
        version: 1,
        models: vec![SnapshotModel {
            service_uid: "service".into(),
            model: "model".into(),
            revision: "r1".into(),
            tokenizer: "tokenizer".into(),
            tokenizer_revision: "r1".into(),
            capabilities: ["chat".into()].into_iter().collect(),
            max_input_tokens: Some(2048),
            targets: vec![ScalingTarget {
                service_uid: "service".into(),
                name: "default".into(),
                uid: "pool".into(),
                kind: ScalingTargetKind::Pool,
            }],
        }],
        groups: vec![],
        pd_components: vec![],
        pd_pipeline_scopes: vec![],
        epd_components: vec![],
        epd_pipeline_scopes: vec![],
    };
    let prepared = builder().build(snapshot).await.unwrap();
    let generation = RuntimeGeneration::new();

    assert!(prepared.publish(&generation));
    assert!(Generation::ready(&generation));
    assert_eq!(generation.configured_models(), vec!["model"]);
}

#[tokio::test]
async fn invalid_snapshot_fails_before_runtime_preparation() {
    let conflicting = br#"{"version":1,"groups":[
        {"route_target_id":"a","model":"model","revision":"r1","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://a","data_parallel_size":1},
        {"route_target_id":"b","model":"model","revision":"r2","tokenizer":"tokenizer","tokenizer_revision":"r1","endpoint":"http://b","data_parallel_size":1}
    ]}"#;
    let builder = builder();
    let snapshot = builder.parse(conflicting).unwrap();

    assert!(matches!(
        builder.build(snapshot).await,
        Err(RuntimeBuildError::InvalidSnapshot(_))
    ));
}
