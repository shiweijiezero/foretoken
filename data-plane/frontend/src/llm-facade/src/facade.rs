// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Request shaping and completion handoffs for encoder and prefill/decode stages.

use foretoken_model_protocol::KvConnector;
use futures::StreamExt;
use vllm_llm::{FinishReason, GenerateRequest};

use crate::http::{MODEL_SERVER_CLIENT, MODEL_SERVER_REQUEST_START_TIMEOUT, bootstrap_engine_id};
use crate::{LlmFacadeError, TokenStream};

/// Consumes the encoder completion barrier and returns its opaque connector descriptor.
pub async fn consume_encoder(stream: TokenStream) -> Result<serde_json::Value, LlmFacadeError> {
    let mut stream = Box::pin(stream);
    while let Some(event) = stream.next().await {
        let event = event?;
        if matches!(
            event.finish_reason,
            Some(FinishReason::Stop(_) | FinishReason::Length)
        ) {
            return event.ec_transfer_params.ok_or(LlmFacadeError::Protocol);
        }
        if event.finish_reason.is_some() {
            return Err(LlmFacadeError::RequestFailed);
        }
    }
    Err(LlmFacadeError::Protocol)
}

/// Retains the Decode request until its selected Prefill has completed the native handoff.
pub struct DecodeHandoff {
    request: GenerateRequest,
    connector: KvConnector,
}

impl DecodeHandoff {
    /// Consumes Prefill's completion barrier and returns the request for fresh Decode selection.
    /// NIXL uses the actual producer descriptor; Mooncake retains its resolved bootstrap identity.
    pub async fn complete_prefill(
        mut self,
        stream: TokenStream,
    ) -> Result<GenerateRequest, LlmFacadeError> {
        let mut stream = Box::pin(stream);
        while let Some(output) = stream.next().await {
            let output = output?;
            match output.finish_reason {
                Some(FinishReason::Length) => {
                    if self.connector == KvConnector::Nixl {
                        let descriptor = output
                            .kv_transfer_params
                            .filter(serde_json::Value::is_object)
                            .ok_or(LlmFacadeError::Protocol)?;
                        self.request
                            .sampling_params
                            .extra_args
                            .get_or_insert_default()
                            .insert("kv_transfer_params".into(), descriptor);
                    }
                    return Ok(self.request);
                }
                Some(_) => return Err(LlmFacadeError::RequestFailed),
                None => {}
            }
        }
        Err(LlmFacadeError::Protocol)
    }
}

/// Rejects transfer descriptors supplied outside the controlled execution workflow.
pub fn reject_client_transfer_params(request: &GenerateRequest) -> Result<(), LlmFacadeError> {
    if request
        .sampling_params
        .extra_args
        .as_ref()
        .is_some_and(|args| {
            args.contains_key("kv_transfer_params") || args.contains_key("ec_transfer_params")
        })
    {
        Err(LlmFacadeError::Configuration)
    } else {
        Ok(())
    }
}

/// Builds the bounded encoder request used as the E/P/D completion barrier.
pub fn encoder_stage_request(
    mut request: GenerateRequest,
) -> Result<GenerateRequest, LlmFacadeError> {
    reject_client_transfer_params(&request)?;
    request.request_id = format!("{}/encoder", request.request_id);
    request.sampling_params.max_tokens = 1;
    request.sampling_params.min_tokens = 1;
    Ok(request)
}

/// Prepares one Prefill stage and retains the matching Decode handoff for the workflow.
/// Only Mooncake queries bootstrap HTTP; NIXL obtains its descriptor from Prefill output.
pub async fn pd_stage_requests(
    request: GenerateRequest,
    connector: KvConnector,
    bootstrap_endpoint: Option<&str>,
    prefill_data_parallel_rank: u32,
) -> Result<(GenerateRequest, DecodeHandoff), LlmFacadeError> {
    reject_client_transfer_params(&request)?;
    let mut prefill_params = serde_json::json!({
        "do_remote_decode": true,
        "do_remote_prefill": false,
    });
    let decode_params = if connector == KvConnector::Mooncake {
        let transfer_id = format!("xfer-{}", request.request_id);
        let endpoint = bootstrap_endpoint.ok_or(LlmFacadeError::Configuration)?;
        let engine_id = tokio::time::timeout(
            MODEL_SERVER_REQUEST_START_TIMEOUT,
            bootstrap_engine_id(&MODEL_SERVER_CLIENT, endpoint, prefill_data_parallel_rank),
        )
        .await
        .map_err(|_| LlmFacadeError::Unavailable)??;
        prefill_params["transfer_id"] = serde_json::json!(transfer_id);
        Some(serde_json::json!({
            "do_remote_decode": false,
            "do_remote_prefill": true,
            "remote_bootstrap_addr": endpoint,
            "remote_engine_id": engine_id,
            "transfer_id": transfer_id,
        }))
    } else {
        None
    };

    let mut prefill = request.clone();
    prefill.request_id = format!("{}/prefill", request.request_id);
    // A length-capped producer retains KV; sampling stops must not terminate its barrier early.
    prefill.sampling_params.max_tokens = 1;
    prefill.sampling_params.min_tokens = 0;
    prefill.sampling_params.eos_token_id = None;
    prefill.sampling_params.stop_token_ids.clear();
    prefill.sampling_params.all_stop_token_ids.clear();
    prefill.sampling_params.structured_outputs = None;
    prefill.sampling_params.repetition_detection = None;
    prefill.sampling_params.thinking_token_budget = None;
    prefill
        .sampling_params
        .extra_args
        .get_or_insert_default()
        .insert("kv_transfer_params".into(), prefill_params);

    let mut decode = request;
    decode.request_id = format!("{}/decode", decode.request_id);
    if let Some(parameters) = decode_params {
        decode
            .sampling_params
            .extra_args
            .get_or_insert_default()
            .insert("kv_transfer_params".into(), parameters);
    }
    Ok((
        prefill,
        DecodeHandoff {
            request: decode,
            connector,
        },
    ))
}

/// Adds an opaque encoder descriptor to the already controlled prefill request.
pub fn inject_ec_transfer_params(prefill: &mut GenerateRequest, descriptor: serde_json::Value) {
    prefill
        .sampling_params
        .extra_args
        .get_or_insert_default()
        .insert("ec_transfer_params".into(), descriptor);
}
