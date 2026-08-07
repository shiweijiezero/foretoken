use std::collections::BTreeMap;
use std::sync::Arc;

use vllm_engine_core_client::protocol::dtype::ModelDtype;
use vllm_engine_core_client::protocol::request::ReasoningParserKwargs;
use vllm_engine_core_client::protocol::sampling::EngineCoreSamplingParams;
use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo, GenerateRequest};

use super::{
    GenerateInput, RuntimeEcTransferMetadata, RuntimeMetadataResponse, RuntimeModelIdentity,
    TokenErrorCode, TokenEvent, TokenOutput, VLLM_PINNED_REVISION,
};

#[test]
fn generate_request_round_trip_preserves_vllm_fields() {
    let request = GenerateRequest {
        request_id: "request-1".into(),
        prompt_token_ids: vec![11, 22, 33],
        sampling_params: EngineCoreSamplingParams::for_test(),
        mm_features: None,
        arrival_time: Some(42.5),
        cache_salt: Some("salt".into()),
        trace_headers: Some(BTreeMap::from([("x-trace-id".into(), "abc".into())])),
        priority: 3,
        data_parallel_rank: Some(2),
        reasoning_parser_kwargs: Some(ReasoningParserKwargs {
            chat_template_kwargs: [("enable_thinking".into(), serde_json::json!(true))].into(),
        }),
        lora_request: None,
    };
    let expected = request.clone();

    let round_trip: GenerateRequest = GenerateInput::from(request).into();

    assert_eq!(round_trip, expected);
}

#[test]
fn generate_input_injects_an_opaque_ec_descriptor_without_replacing_extra_args() {
    let mut input = GenerateInput::from(GenerateRequest {
        request_id: "request-1".into(),
        prompt_token_ids: vec![11],
        sampling_params: EngineCoreSamplingParams {
            extra_args: Some([("existing".into(), serde_json::json!({"preserved": true}))].into()),
            ..EngineCoreSamplingParams::default()
        },
        mm_features: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        reasoning_parser_kwargs: None,
        lora_request: None,
    });
    let descriptor = serde_json::json!({"opaque": [1, {"nested": true}]});

    input.inject_ec_transfer_params(descriptor.clone()).unwrap();
    assert_eq!(
        input.sampling_params.extra_args.as_ref().unwrap()["ec_transfer_params"],
        descriptor
    );
    assert_eq!(
        input.sampling_params.extra_args.as_ref().unwrap()["existing"],
        serde_json::json!({"preserved": true})
    );
    assert!(
        input
            .inject_ec_transfer_params(serde_json::json!({}))
            .is_err()
    );
}

#[test]
fn runtime_metadata_has_a_versioned_wire_shape() {
    let metadata = RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: "model".into(),
            revision: "r1".into(),
        },
        model_dtype: ModelDtype::BFloat16,
        effective_max_model_len: 32_768,
        vllm_pinned_revision: VLLM_PINNED_REVISION.into(),
        vllm_version: "0.0.0".into(),
        ec_transfer: None,
        capabilities: Default::default(),
    };

    assert_eq!(
        serde_json::to_string(&metadata).unwrap(),
        r#"{"version":1,"model":{"model":"model","revision":"r1"},"model_dtype":"bfloat16","effective_max_model_len":32768,"vllm_pinned_revision":"5b14019576475224d86044b262e28a04a85d4086","vllm_version":"0.0.0","ec_transfer":null,"capabilities":[]}"#,
    );
    assert!(serde_json::from_str::<RuntimeMetadataResponse>(
        r#"{"version":1,"model":{"model":"model","revision":"r1"},"model_dtype":"bfloat16","effective_max_model_len":32768,"vllm_pinned_revision":"5b14019576475224d86044b262e28a04a85d4086","vllm_version":"0.0.0","ec_transfer":null,"capabilities":[],"extra":true}"#
    )
    .is_err());
}

#[test]
fn runtime_metadata_reports_ec_identity_and_capability() {
    let metadata = RuntimeMetadataResponse {
        version: 1,
        model: RuntimeModelIdentity {
            model: "model".into(),
            revision: "r1".into(),
        },
        model_dtype: ModelDtype::BFloat16,
        effective_max_model_len: 32_768,
        vllm_pinned_revision: VLLM_PINNED_REVISION.into(),
        vllm_version: "0.0.0".into(),
        ec_transfer: Some(RuntimeEcTransferMetadata {
            role: "ec_producer".into(),
            profile: "validated-pynccl".into(),
            connector: "PyNcclConnector".into(),
            fingerprint: "a-fingerprint".into(),
        }),
        capabilities: ["ec_transfer".into()].into_iter().collect(),
    };

    assert_eq!(
        serde_json::to_string(&metadata).unwrap(),
        r#"{"version":1,"model":{"model":"model","revision":"r1"},"model_dtype":"bfloat16","effective_max_model_len":32768,"vllm_pinned_revision":"5b14019576475224d86044b262e28a04a85d4086","vllm_version":"0.0.0","ec_transfer":{"role":"ec_producer","profile":"validated-pynccl","connector":"PyNcclConnector","fingerprint":"a-fingerprint"},"capabilities":["ec_transfer"]}"#,
    );
}

#[test]
fn token_error_event_has_a_stable_wire_shape() {
    let event = TokenEvent::Error {
        request_id: "request-1".into(),
        code: TokenErrorCode::Unavailable,
    };

    assert_eq!(
        serde_json::to_string(&event).unwrap(),
        r#"{"type":"error","request_id":"request-1","code":"unavailable"}"#,
    );
}

#[test]
fn generate_output_round_trip_preserves_transfer_metadata() {
    let output = GenerateOutput {
        request_id: "request-1".into(),
        prompt_info: Some(GeneratePromptInfo {
            prompt_token_ids: Arc::from([11, 22, 33]),
            prompt_logprobs: None,
        }),
        token_ids: vec![44, 55],
        logprobs: None,
        finish_reason: Some(FinishReason::stop_eos()),
        cached_token_count: 2,
        kv_transfer_params: Some(serde_json::json!({"connector": "mooncake"})),
        ec_transfer_params: Some(serde_json::json!({"encoder": true})),
    };
    let expected = output.clone();

    let round_trip: GenerateOutput = TokenOutput::from(output).into();

    assert_eq!(round_trip, expected);
}
