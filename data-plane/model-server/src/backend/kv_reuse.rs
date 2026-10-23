// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Remote KV acquisition and cleanup owned by the receiving execution.

use foretoken_model_protocol::{
    GenerateInput, KV_PREFIX_CANCEL_PATH, KV_PREFIX_CLAIM_PATH, KvPrefixCancel,
    KvPrefixCancelResult, KvPrefixClaim, KvPrefixClaimResult, KvPrefixRequest, KvReuseSource,
};
use tokio::runtime::Handle;
use tokio_util::task::TaskTracker;
use tracing::warn;

use crate::backend::{BackendError, ExecutionControl};

/// Retains a remote claim's cleanup identity even when its acquisition response is lost.
pub(crate) struct RemoteKvRead {
    cancel: KvPrefixCancel,
    client: reqwest::Client,
    runtime: Handle,
    cleanup: TaskTracker,
    cancel_needed: bool,
}

impl RemoteKvRead {
    /// Binds an accepted execution to a source and the receiver's cleanup tracker.
    pub(crate) fn new(
        source: KvReuseSource,
        consumer_id: String,
        client: reqwest::Client,
        cleanup: TaskTracker,
    ) -> Self {
        Self {
            cancel: KvPrefixCancel {
                source,
                consumer_id,
            },
            client,
            runtime: Handle::current(),
            cleanup,
            cancel_needed: false,
        }
    }

    /// Claims and injects a native descriptor; false lets the caller proceed with ordinary Prefill.
    pub(crate) async fn prepare(
        &mut self,
        request: &mut GenerateInput,
        execution: &ExecutionControl,
    ) -> Result<bool, BackendError> {
        if request.lora_request.is_some()
            || request
                .mm_features
                .as_ref()
                .is_some_and(|features| !features.is_empty())
        {
            return Err(BackendError::InvalidRequest);
        }
        let claim = KvPrefixClaim {
            source: self.cancel.source.clone(),
            consumer_id: self.cancel.consumer_id.clone(),
            prefix: KvPrefixRequest {
                prompt_token_ids: request.prompt_token_ids.clone(),
                dp_rank: self.cancel.source.dp_rank,
                cache_salt: request.cache_salt.clone(),
                lora_name: None,
                lora_path: None,
                multimodal: Vec::new(),
            },
        };
        let endpoint = self
            .cancel
            .source
            .model_server_endpoint
            .trim_end_matches('/');
        let claim = self
            .client
            .post(format!("{endpoint}{KV_PREFIX_CLAIM_PATH}"))
            .json(&claim)
            .build()
            .map_err(|_| BackendError::InvalidRequest)?;
        let response = tokio::select! {
            biased;
            _ = execution.cancelled() => return Err(BackendError::Rejected),
            response = async {
                self.cancel_needed = true;
                self.client.execute(claim).await
            } => response,
        };
        let response = match response {
            Ok(response) => response,
            // Redirects are disabled, so a definite connection failure precedes claim delivery.
            Err(error) if error.is_connect() && !error.is_timeout() => {
                self.cancel_needed = false;
                return Ok(false);
            }
            Err(_) => return Ok(false),
        };
        if response.status().is_server_error()
            || matches!(
                response.status(),
                reqwest::StatusCode::NOT_FOUND | reqwest::StatusCode::GONE
            )
        {
            return Ok(false);
        }
        if !response.status().is_success() {
            return Err(BackendError::Protocol);
        }
        let result = tokio::select! {
            biased;
            _ = execution.cancelled() => return Err(BackendError::Rejected),
            result = response.json::<KvPrefixClaimResult>() => {
                result.map_err(|_| BackendError::Protocol)?
            },
        };
        match result {
            KvPrefixClaimResult::Miss => {
                self.cancel_needed = false;
                Ok(false)
            }
            KvPrefixClaimResult::Granted { descriptor } => {
                let serde_json::Value::Object(descriptor) = descriptor else {
                    return Err(BackendError::Protocol);
                };
                // Retain the workflow's outgoing handoff identity while adding the source to read.
                let parameters = request
                    .sampling_params
                    .extra_args
                    .get_or_insert_default()
                    .entry("kv_transfer_params".into())
                    .or_insert_with(|| serde_json::json!({}));
                parameters
                    .as_object_mut()
                    .ok_or(BackendError::Protocol)?
                    .extend(descriptor);
                Ok(true)
            }
        }
    }
}

impl Drop for RemoteKvRead {
    fn drop(&mut self) {
        if !self.cancel_needed {
            return;
        }
        let endpoint = format!(
            "{}{KV_PREFIX_CANCEL_PATH}",
            self.cancel
                .source
                .model_server_endpoint
                .trim_end_matches('/')
        );
        let cancel = self.cancel.clone();
        let client = self.client.clone();
        // Acquisition or cancellation responses may be lost. Retain the claim identity
        // until the source confirms release; native completion resolves InUse reads.
        self.cleanup.spawn_on(
            async move {
                let mut reported_failure = false;
                loop {
                    let result = async {
                        client
                            .post(&endpoint)
                            .json(&cancel)
                            .send()
                            .await?
                            .error_for_status()?
                            .json::<KvPrefixCancelResult>()
                            .await
                    }
                    .await;
                    match result {
                        Ok(KvPrefixCancelResult::Released | KvPrefixCancelResult::Miss) => return,
                        Ok(KvPrefixCancelResult::InUse) => {}
                        Err(_) if !reported_failure => {
                            reported_failure = true;
                            warn!(
                                instance_id = %cancel.source.instance_id,
                                consumer_id = %cancel.consumer_id,
                                "KV source cancellation remains unconfirmed; retaining cleanup ownership"
                            );
                        }
                        Err(_) => {}
                    }
                    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
                }
            },
            &self.runtime,
        );
    }
}
