// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use foretoken_model_server::launch::LaunchPlanV1;

fn plan() -> LaunchPlanV1 {
    LaunchPlanV1::parse(r#"{"version":1,"nodeCount":1,"artifacts":{"model":"model","revision":"rev","tokenizer":"tokenizer","tokenizerRevision":"tokenizer-rev"},"parallelism":{"tp":2,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"lifecycle":{"startupSeconds":30,"drainSeconds":7},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":["--max-model-len=32768"]}"#).unwrap()
}

// Protects launch from unsupported node and context-parallel topology combinations.
#[test]
fn rejects_invalid_topology() {
    let mut invalid = plan();
    invalid.node_count = 2;
    assert!(invalid.validate().is_err());

    let mut invalid = plan();
    invalid.parallelism.pcp = 2;
    invalid.parallelism.dp = 2;
    assert!(invalid.validate().is_err());
}

// Protects controller-owned vLLM flags from duplicate rendering.
#[test]
fn renders_owned_arguments_once() {
    let args = plan().render_vllm_args().unwrap();
    for flag in [
        "--revision=",
        "--tokenizer=",
        "--tensor-parallel-size=",
        "--pipeline-parallel-size=",
        "--prefill-context-parallel-size=",
        "--decode-context-parallel-size=",
        "--shutdown-timeout=",
    ] {
        assert_eq!(
            args.iter().filter(|arg| arg.starts_with(flag)).count(),
            1,
            "{flag}: {args:?}"
        );
    }
    assert!(args.iter().any(|arg| arg == "--max-model-len=32768"));

    let event_config = args
        .iter()
        .find_map(|arg| arg.strip_prefix("--kv-events-config="))
        .expect("KV event config");
    let event_config: serde_json::Value = serde_json::from_str(event_config).unwrap();
    assert_eq!(
        event_config["endpoint"],
        "ipc:///tmp/foretoken-kv-events.sock"
    );
    assert_eq!(event_config["topic"], "foretoken-kv-v1");
}

// Protects role-specific EC launch configuration for encoder and prefill.
#[test]
fn ec_plan_renders_one_owned_config_for_each_role() {
    let producer = LaunchPlanV1::parse(r#"{"version":1,"nodeCount":1,"artifacts":{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"verified-ec","profileRevision":"r1","connector":"ECExampleConnector","role":"producer","sharedStoragePath":"/mnt/foretoken/ec"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[]}"#).unwrap();
    let consumer = LaunchPlanV1::parse(r#"{"version":1,"nodeCount":1,"artifacts":{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"verified-ec","profileRevision":"r1","connector":"ECExampleConnector","role":"consumer","sharedStoragePath":"/mnt/foretoken/ec"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[]}"#).unwrap();

    let args = producer.render_vllm_args().unwrap();
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
            .render_vllm_args()
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
    let invalid = r#"{"version":1,"nodeCount":1,"artifacts":{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"profile","profileRevision":"r1","connector":"arbitrary","role":"producer","sharedStoragePath":"relative"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[]}"#;
    assert!(LaunchPlanV1::parse(invalid).is_err());
}

// Protects each KV launch variant and its owned runtime arguments.
#[test]
fn kv_variants_render_expected_semantics() {
    let cases = [
        (
            r#"{"kind":"pd","role":"kv_consumer","protocol":"rdma","deviceName":"mlx5_1","events":true}"#,
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
            r#"{{"version":1,"nodeCount":1,"artifacts":{{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"}},"parallelism":{{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1}},"kv":{kv},"lifecycle":{{"startupSeconds":1,"drainSeconds":1}},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[]}}"#
        );
        let rendered = LaunchPlanV1::parse(&source)
            .unwrap()
            .render_vllm_args()
            .unwrap();
        assert!(
            rendered.iter().any(|arg| arg.contains(want)),
            "{rendered:?}"
        );
        if want == "MooncakeConnector" || want == "MultiConnector" {
            assert!(
                rendered
                    .iter()
                    .any(|arg| arg.contains(r#""device_name":"mlx5_1""#)),
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
            if want == "TieringOffloadingSpec" {
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
