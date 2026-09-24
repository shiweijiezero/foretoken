// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::path::Path;

use foretoken_artifacts::ModelSource;
use foretoken_model_server::launch::LaunchPlanV1;

fn plan() -> LaunchPlanV1 {
    LaunchPlanV1::parse(r#"{"version":1,"nodeCount":1,"artifacts":{"source":"hf","model":"model","revision":"rev","tokenizer":"tokenizer","tokenizerRevision":"tokenizer-rev"},"parallelism":{"tp":2,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"lifecycle":{"startupSeconds":30,"drainSeconds":7},"internalGenerateRequestBodyLimitBytes":67108864,"engineArgs":{"max-model-len":32768,"dtype":"bfloat16","quantization":"awq","kv-cache-dtype":"fp8","gpu-memory-utilization":0.8,"max-num-seqs":16,"max-num-batched-tokens":2048,"enforce-eager":false,"speculative-config":{"method":"eagle3","model":"draft/model","num_speculative_tokens":2},"compilation-config":{"mode":3}}}"#).unwrap()
}

// Protects launch from malformed topology values while vLLM owns legal combinations.
#[test]
fn rejects_malformed_topology() {
    let mut invalid = plan();
    invalid.node_count = 3;
    assert!(invalid.validate().is_err());

    let mut invalid = plan();
    invalid.parallelism.pcp = 0;
    assert!(invalid.validate().is_err());
}

// Protects the supported controller-owned vLLM argument contract.
#[test]
fn renders_supported_owned_arguments() {
    let args = plan().render_vllm_args(None).unwrap();
    for flag in [
        "--revision=",
        "--tokenizer=",
        "--tensor-parallel-size=",
        "--pipeline-parallel-size=",
        "--prefill-context-parallel-size=",
        "--decode-context-parallel-size=",
    ] {
        assert_eq!(
            args.iter().filter(|arg| arg.starts_with(flag)).count(),
            1,
            "{flag}: {args:?}"
        );
    }
    for argument in [
        "--max-model-len=32768",
        "--dtype=bfloat16",
        "--quantization=awq",
        "--kv-cache-dtype=fp8",
        "--gpu-memory-utilization=0.8",
        "--max-num-seqs=16",
        "--max-num-batched-tokens=2048",
        "--no-enforce-eager",
        r#"--compilation-config={"mode":3}"#,
    ] {
        assert!(args.iter().any(|arg| arg == argument), "{args:?}");
    }
    let speculative = args
        .iter()
        .find_map(|arg| arg.strip_prefix("--speculative-config="))
        .expect("speculative config");
    let speculative: serde_json::Value = serde_json::from_str(speculative).unwrap();
    assert_eq!(speculative["method"], "eagle3");
    assert_eq!(speculative["model"], "draft/model");
    assert_eq!(speculative["num_speculative_tokens"], 2);
    assert!(
        !args
            .iter()
            .any(|arg| arg.starts_with("--shutdown-timeout=")),
        "{args:?}"
    );

    let event_config = args
        .iter()
        .find_map(|arg| arg.strip_prefix("--kv-events-config="))
        .expect("KV event config");
    let event_config: serde_json::Value = serde_json::from_str(event_config).unwrap();
    assert_eq!(event_config["endpoint"], "tcp://127.0.0.1:30100");
    assert_eq!(event_config["topic"], "foretoken-kv-v1");

    let mut local = plan();
    local.artifacts.source = ModelSource::Local;
    let local_args = local.render_vllm_args(None).unwrap();
    assert!(
        !local_args
            .iter()
            .any(|arg| arg.starts_with("--revision=") || arg.starts_with("--tokenizer-revision="))
    );

    let mut modelscope = plan();
    modelscope.artifacts.source = ModelSource::ModelScope;
    let environment = modelscope.source_environment(Path::new("/models"));
    assert!(environment.contains(&("VLLM_USE_MODELSCOPE".into(), "true".into())));
    assert!(environment.contains(&("MODELSCOPE_CACHE".into(), "/models/modelscope".into())));
    assert!(environment.contains(&("MODELSCOPE_DOMAIN".into(), "www.modelscope.cn".into())));
}

// Protects role-specific EC launch configuration for encoder and prefill.
#[test]
fn ec_plan_renders_one_owned_config_for_each_role() {
    let producer = LaunchPlanV1::parse(r#"{"version":1,"nodeCount":1,"artifacts":{"source":"hf","model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"verified-ec","profileRevision":"r1","connector":"ECExampleConnector","role":"producer","sharedStoragePath":"/mnt/foretoken/ec"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"engineArgs":{}}"#).unwrap();
    let consumer = LaunchPlanV1::parse(r#"{"version":1,"nodeCount":1,"artifacts":{"source":"hf","model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"verified-ec","profileRevision":"r1","connector":"ECExampleConnector","role":"consumer","sharedStoragePath":"/mnt/foretoken/ec"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"engineArgs":{}}"#).unwrap();

    let args = producer.render_vllm_args(None).unwrap();
    let rendered: Vec<_> = args
        .iter()
        .filter(|arg| arg.starts_with("--ec-transfer-config="))
        .collect();
    assert_eq!(rendered.len(), 1, "{args:?}");
    let config: serde_json::Value =
        serde_json::from_str(rendered[0].strip_prefix("--ec-transfer-config=").unwrap()).unwrap();
    assert_eq!(config["ec_role"], "ec_producer");
    assert!(args.iter().any(|arg| arg == "--no-enable-prefix-caching"));
    assert!(
        !consumer
            .render_vllm_args(None)
            .unwrap()
            .iter()
            .any(|arg| arg == "--no-enable-prefix-caching")
    );
    assert_eq!(
        producer.ec.runtime_metadata().unwrap().profile,
        consumer.ec.runtime_metadata().unwrap().profile
    );
    assert_eq!(producer.ec.runtime_metadata().unwrap().role, "ec_producer");
}

// Protects E/P/D launch from incomplete or mismatched EC configuration.
#[test]
fn rejects_invalid_ec_pairing() {
    let invalid = r#"{"version":1,"nodeCount":1,"artifacts":{"source":"hf","model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"profile","profileRevision":"r1","connector":"arbitrary","role":"producer","sharedStoragePath":"relative"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"engineArgs":{}}"#;
    assert!(LaunchPlanV1::parse(invalid).is_err());
}

// Protects each KV launch variant and its owned runtime arguments.
#[test]
fn kv_variants_render_expected_semantics() {
    let cases = [
        (
            r#"{"kind":"pd","role":"kv_consumer","protocol":"rdma","events":true}"#,
            "MooncakeConnector",
        ),
        (
            r#"{"kind":"cpuOffload","cpuBytes":9,"events":true}"#,
            "CPUOffloadingSpec",
        ),
        (
            r#"{"kind":"filesystemOffload","cpuBytes":9,"storagePath":"/mnt/foretoken/kv-offload","events":true}"#,
            "TieringOffloadingSpec",
        ),
        (
            r#"{"kind":"mooncakeStore","role":"kv_both","events":true}"#,
            "MooncakeStoreConnector",
        ),
        (
            r#"{"kind":"multiConnector","role":"kv_producer","protocol":"rdma","deviceName":"mlx5_1","events":true}"#,
            "MultiConnector",
        ),
    ];
    for (kv, want) in cases {
        let source = format!(
            r#"{{"version":1,"nodeCount":1,"artifacts":{{"source":"hf","model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"}},"parallelism":{{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1}},"kv":{kv},"lifecycle":{{"startupSeconds":1,"drainSeconds":1}},"internalGenerateRequestBodyLimitBytes":67108864,"engineArgs":{{}}}}"#
        );
        let rendered = LaunchPlanV1::parse(&source)
            .unwrap()
            .render_vllm_args(None)
            .unwrap();
        assert!(
            rendered.iter().any(|arg| arg.contains(want)),
            "{rendered:?}"
        );
        if want == "MooncakeConnector" || want == "MultiConnector" {
            let device_name = if want == "MooncakeConnector" {
                ""
            } else {
                "mlx5_1"
            };
            assert!(
                rendered
                    .iter()
                    .any(|arg| arg.contains(&format!(r#""device_name":"{device_name}""#))),
                "{rendered:?}"
            );
        }
        if want == "CPUOffloadingSpec" || want == "TieringOffloadingSpec" {
            let config = rendered
                .iter()
                .find_map(|arg| arg.strip_prefix("--kv-transfer-config="))
                .expect("KV transfer config");
            let config: serde_json::Value = serde_json::from_str(config).unwrap();
            assert_eq!(config["kv_connector_extra_config"]["spec_name"], want);
            if want == "CPUOffloadingSpec" {
                assert_eq!(
                    config["kv_connector_extra_config"]["self_describing_kv_events"],
                    true
                );
            } else {
                assert!(config["kv_connector_extra_config"]["self_describing_kv_events"].is_null());
            }
            if want == "TieringOffloadingSpec" {
                assert_eq!(
                    config["kv_connector_module_path"],
                    foretoken_model_server::shared_kv::OFFLOADING_CONNECTOR_MODULE
                );
                assert_eq!(
                    config["kv_connector_extra_config"]["secondary_tiers"][0]["root_dir"],
                    "/mnt/foretoken/kv-offload"
                );
                assert_eq!(
                    config["kv_connector_extra_config"]["secondary_tiers"][0]["enable_kv_events"],
                    true
                );
            }
        }
    }
}
