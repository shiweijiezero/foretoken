// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Reusable request shaping and bounded stage-stream consumption.

use futures::StreamExt;
use vllm_llm::{FinishReason, GenerateRequest};

use crate::http::{MODEL_SERVER_REQUEST_START_TIMEOUT, bootstrap_engine_id};
use crate::{LlmFacadeError, TokenStream};

/// Consumes the encoder completion barrier and returns its opaque connector descriptor.
pub async fn consume_encoder(stream: TokenStream) -> Result<serde_json::Value, LlmFacadeError> {
    let mut stream = Box::pin(stream);
    while let Some(event) = stream.next().await {
        let event = event?;
        if event.finish_reason == Some(FinishReason::Length) {
            return event.ec_transfer_params.ok_or(LlmFacadeError::Protocol);
        }
        if event.finish_reason.is_some() {
            return Err(LlmFacadeError::RequestFailed);
        }
    }
    Err(LlmFacadeError::Protocol)
}

/// Consumes the prefill completion barrier.
pub async fn consume_prefill(stream: TokenStream) -> Result<(), LlmFacadeError> {
    let mut stream = Box::pin(stream);
    while let Some(event) = stream.next().await {
        match event?.finish_reason {
            Some(FinishReason::Length) => return Ok(()),
            Some(_) => return Err(LlmFacadeError::RequestFailed),
            None => {}
        }
    }
    Err(LlmFacadeError::Protocol)
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

/// Builds the selected P/D stage requests with connector-owned KV transfer parameters.
pub async fn pd_stage_requests(
    request: GenerateRequest,
    bootstrap_endpoint: &str,
) -> Result<(GenerateRequest, GenerateRequest), LlmFacadeError> {
    let client = reqwest::Client::builder()
        .timeout(MODEL_SERVER_REQUEST_START_TIMEOUT)
        .build()
        .map_err(|_| LlmFacadeError::Configuration)?;
    let engine_id = bootstrap_engine_id(&client, bootstrap_endpoint).await?;
    pd_requests_with_engine(request, bootstrap_endpoint, engine_id)
}

fn pd_requests_with_engine(
    request: GenerateRequest,
    bootstrap_endpoint: &str,
    engine_id: String,
) -> Result<(GenerateRequest, GenerateRequest), LlmFacadeError> {
    // Prefill produces one transfer and decode consumes that exact transfer using the live
    // prefill engine identity. Stage IDs and transfer parameters are workflow-owned.
    reject_client_transfer_params(&request)?;
    let transfer_id = format!("xfer-{}", request.request_id);
    let mut prefill = request.clone();
    prefill.request_id = format!("{}/prefill", request.request_id);
    prefill.sampling_params.max_tokens = 1;
    prefill.sampling_params.min_tokens = 1;
    let mut prefill_args = prefill
        .sampling_params
        .extra_args
        .take()
        .unwrap_or_default();
    prefill_args.insert("kv_transfer_params".into(), serde_json::json!({"do_remote_decode": true, "do_remote_prefill": false, "transfer_id": transfer_id}));
    prefill.sampling_params.extra_args = Some(prefill_args);
    let mut decode = request;
    decode.request_id = format!("{}/decode", decode.request_id);
    let mut decode_args = decode.sampling_params.extra_args.take().unwrap_or_default();
    decode_args.insert("kv_transfer_params".into(), serde_json::json!({"do_remote_decode": false, "do_remote_prefill": true, "remote_bootstrap_addr": bootstrap_endpoint, "remote_engine_id": engine_id, "transfer_id": transfer_id}));
    decode.sampling_params.extra_args = Some(decode_args);
    Ok((prefill, decode))
}

/// Adds an opaque encoder descriptor to the already controlled prefill request.
pub fn inject_ec_transfer_params(prefill: &mut GenerateRequest, descriptor: serde_json::Value) {
    prefill
        .sampling_params
        .extra_args
        .get_or_insert_default()
        .insert("ec_transfer_params".into(), descriptor);
}
