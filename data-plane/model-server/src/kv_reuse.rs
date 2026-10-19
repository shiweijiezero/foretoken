// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! 由接收执行持有的远端 KV 领取及其清理。

use foretoken_model_protocol::{
    GenerateInput, KV_PREFIX_CANCEL_PATH, KV_PREFIX_CLAIM_PATH, KvPrefixCancel,
    KvPrefixCancelResult, KvPrefixClaim, KvPrefixClaimResult, KvPrefixRequest, KvReuseSource,
};
use tokio::runtime::Handle;
use tracing::warn;

use crate::backend::{BackendError, ExecutionControl};

/// 在请求提交前建立清理身份，领取响应丢失时仍可撤销同一次操作。
pub(crate) struct RemoteKvRead {
    endpoint: String,
    cancel: KvPrefixCancel,
    client: reqwest::Client,
    runtime: Handle,
    cleanup: tokio_util::task::TaskTracker,
    cancel_needed: bool,
}

impl RemoteKvRead {
    /// 接收 model-server 为已接纳执行绑定来源，保留清理责任直到原生请求结束。
    pub(crate) fn new(
        source: KvReuseSource,
        consumer_id: String,
        client: reqwest::Client,
        cleanup: tokio_util::task::TaskTracker,
    ) -> Self {
        Self {
            endpoint: source
                .model_server_endpoint
                .trim_end_matches('/')
                .to_owned(),
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

    /// 领取并注入原生描述符，返回是否采用来源；未命中时调用方可立即清理并正常计算。
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
        let claim = self
            .client
            .post(format!("{}{KV_PREFIX_CLAIM_PATH}", self.endpoint))
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
            // 固定物理地址且禁止重定向；明确建连失败时请求尚未发送，没有领取需要撤销。
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
                if !descriptor.is_object() {
                    return Err(BackendError::Protocol);
                }
                request
                    .sampling_params
                    .extra_args
                    .get_or_insert_default()
                    .insert("kv_transfer_params".into(), descriptor);
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
        let endpoint = self.endpoint.clone();
        let cancel = self.cancel.clone();
        let client = self.client.clone();
        // 撤销响应可能丢失。保持同一领取身份直到来源确认结束；InUse 由 native 完成通知释放。
        self.cleanup.spawn_on(
            async move {
                let mut reported_failure = false;
                loop {
                    let result = async {
                        client
                            .post(format!("{endpoint}{KV_PREFIX_CANCEL_PATH}"))
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
