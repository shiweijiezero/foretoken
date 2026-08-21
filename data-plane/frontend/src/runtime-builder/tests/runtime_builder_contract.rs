// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::Arc;

use foretoken_backend_registry::{ServingSnapshot, SnapshotModel};
use foretoken_router::{RouteTargetSet, RouterPipelineConfig, ScalingTarget, ScalingTargetKind};
use foretoken_runtime_builder::{KvIndexCredential, RuntimeBuildError, RuntimeBuilder};
use foretoken_server::{Generation, GenerationRequest, RuntimeGeneration};
use foretoken_text::{Prompt, SamplingParams, TextDecodeOptions};

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
            admission_target_sets: ["pool-a", "pool-b"]
                .into_iter()
                .map(|uid| {
                    RouteTargetSet::new(vec![ScalingTarget {
                        service_uid: "service".into(),
                        name: uid.into(),
                        uid: uid.into(),
                        kind: ScalingTargetKind::Pool,
                    }])
                })
                .collect(),
        }],
        groups: vec![],
        pd_components: vec![],
        pd_pipeline_scopes: vec![],
        epd_components: vec![],
        epd_pipeline_scopes: vec![],
    };
    let prepared = builder().build(snapshot).await.unwrap();
    let generation = Arc::new(RuntimeGeneration::new(std::time::Duration::from_secs(1)));

    assert!(prepared.publish(&generation));
    assert!(Generation::ready(&*generation));
    assert_eq!(generation.configured_models(), vec!["model"]);

    let cold_request = |request_id: &str| GenerationRequest {
        model: "model".into(),
        request_id: request_id.into(),
        prompt: Prompt::Text("hello".into()),
        sampling_params: SamplingParams::default(),
        decode_options: TextDecodeOptions::default(),
        intermediate: false,
        priority: 0,
        cache_salt: None,
        session_id: None,
        arrival_time: None,
        tool_call_parser: Default::default(),
        reasoning_parser: Default::default(),
    };
    let requests = ["cold-a", "cold-b"].map(|request_id| {
        let generation = generation.clone();
        let request = cold_request(request_id);
        tokio::spawn(async move { generation.generate(request).await })
    });
    tokio::time::timeout(std::time::Duration::from_secs(1), async {
        loop {
            let telemetry = foretoken_metrics::autoscaling_telemetry();
            if ["pool-a", "pool-b"].into_iter().all(|target_id| {
                telemetry.targets.iter().any(|target| {
                    target.target.target_id == target_id && target.queued_requests == 1
                })
            }) {
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    for request in requests {
        request.abort();
        let _ = request.await;
    }
    let telemetry = foretoken_metrics::autoscaling_telemetry();
    assert!(["pool-a", "pool-b"].into_iter().all(|target_id| {
        telemetry
            .targets
            .iter()
            .any(|target| target.target.target_id == target_id && target.queued_requests == 0)
    }));
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
