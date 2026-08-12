use std::collections::HashMap;

use super::RuntimeConfig;

fn values() -> HashMap<String, String> {
    [("FORETOKEN_VLLM_LAUNCH_PLAN", r#"{"version":1,"artifacts":{"model":"model","revision":"main","tokenizer":"tokenizer","tokenizerRevision":"main"},"parallelism":{"tp":1,"pp":1,"dp":2,"pcp":1,"dcp":1},"kv":{"kind":"none","events":false},"lifecycle":{"startupSeconds":30,"drainSeconds":7},"internalGenerateRequestBodyLimitBytes":67108864,"extraArgs":["--max-model-len=42"]}"#), ("FORETOKEN_INTERNAL_LISTEN", "0.0.0.0:8080")].into_iter().map(|(key, value)| (key.into(), value.into())).collect()
}

#[test]
fn requires_controller_owned_plan_and_listener() {
    for name in ["FORETOKEN_VLLM_LAUNCH_PLAN", "FORETOKEN_INTERNAL_LISTEN"] {
        let mut input = values();
        input.remove(name);
        assert!(
            RuntimeConfig::from_values(&input)
                .unwrap_err()
                .contains(name)
        );
    }
}

#[test]
fn managed_config_has_single_argv_owner() {
    let config = RuntimeConfig::from_values(&values()).unwrap();
    let engine = config.launch.managed_engine(1234).unwrap();
    assert_eq!(engine.model, "model");
    assert_eq!(engine.data_parallel_size, 2);
    assert_eq!(
        engine.python_args,
        [
            "--revision=main",
            "--tokenizer=tokenizer",
            "--tokenizer-revision=main",
            "--tensor-parallel-size=1",
            "--pipeline-parallel-size=1",
            "--prefill-context-parallel-size=1",
            "--decode-context-parallel-size=1",
            "--max-model-len=42",
            "--shutdown-timeout=7"
        ]
    );
}
