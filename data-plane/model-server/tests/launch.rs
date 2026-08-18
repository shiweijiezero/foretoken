// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use foretoken_model_server::launch::LaunchPlanV1;

fn plan() -> LaunchPlanV1 {
    LaunchPlanV1::parse(r#"{"version":1,"artifacts":{"model":"model","revision":"rev","tokenizer":"tokenizer","tokenizerRevision":"tokenizer-rev"},"parallelism":{"tp":2,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"lifecycle":{"startupSeconds":30,"drainSeconds":7},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":["--max-model-len=32768"]}"#).unwrap()
}

#[test]
fn rejects_unknown_and_missing_wire_fields() {
    assert!(LaunchPlanV1::parse(r#"{"version":1,"artifacts":{},"parallelism":{},"kv":{"kind":"none","events":true},"lifecycle":{},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[],"unexpected":true}"#).is_err());
    assert!(LaunchPlanV1::parse(r#"{"version":1,"artifacts":{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"lifecycle":{"startupSeconds":1,"drainSeconds":1}}"#).is_err());
}

#[test]
fn rejects_invalid_topology_and_extra_arg_bypass() {
    let mut invalid = plan();
    invalid.parallelism.pcp = 2;
    invalid.parallelism.dp = 2;
    assert!(invalid.validate().is_err());
    for argument in [
        "--model=other",
        "-dp=2",
        "--max_model_len=1",
        "--config=x",
        "--max-model-len 1",
        "--unknown=1",
    ] {
        let mut bad = plan();
        bad.extra_args = vec![argument.into()];
        assert!(bad.validate().is_err(), "{argument} was accepted");
    }
}

#[test]
fn rejects_out_of_range_internal_generate_request_body_limits() {
    for limit in [1_048_575, 268_435_457] {
        let mut invalid = plan();
        invalid.internal_generate_request_body_limit_bytes = limit;
        assert!(invalid.validate().is_err(), "limit {limit} was accepted");
    }
}

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
}

#[test]
fn ec_plan_renders_one_owned_config_for_each_role() {
    let producer = LaunchPlanV1::parse(r#"{"version":1,"artifacts":{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"verified-ec","profileRevision":"r1","connector":"ECExampleConnector","role":"producer","sharedStoragePath":"/var/lib/foretoken/ec"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[]}"#).unwrap();
    let consumer = LaunchPlanV1::parse(r#"{"version":1,"artifacts":{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"verified-ec","profileRevision":"r1","connector":"ECExampleConnector","role":"consumer","sharedStoragePath":"/var/lib/foretoken/ec"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[]}"#).unwrap();

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

#[test]
fn rejects_invalid_ec_pairing_and_ec_extra_arg_bypass() {
    let invalid = r#"{"version":1,"artifacts":{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"},"parallelism":{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1},"kv":{"kind":"none","events":true},"ec":{"profileName":"profile","profileRevision":"r1","connector":"arbitrary","role":"producer","sharedStoragePath":"relative"},"lifecycle":{"startupSeconds":1,"drainSeconds":1},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[]}"#;
    assert!(LaunchPlanV1::parse(invalid).is_err());

    let mut bypass = plan();
    bypass.extra_args = vec!["--ec-transfer-config={}".into()];
    assert!(bypass.validate().is_err());
}

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
            r#"{"kind":"filesystemOffload","cpuBytes":9,"events":true}"#,
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
            r#"{{"version":1,"artifacts":{{"model":"m","revision":"r","tokenizer":"t","tokenizerRevision":"tr"}},"parallelism":{{"tp":1,"pp":1,"dp":1,"pcp":1,"dcp":1}},"kv":{kv},"lifecycle":{{"startupSeconds":1,"drainSeconds":1}},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":[]}}"#
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
    }
}
