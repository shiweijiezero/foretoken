// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Reads shared prefix observations from each DP rank's active native Store connector.

use foretoken_model_protocol::{KvPlacement, KvSharedPrefixRequest, KvSharedPrefixResponse};
use serde::Deserialize;
use zeromq::prelude::{Socket, SocketRecv, SocketSend};

const LOOKUP_BASE_PORT: u32 = 30200;

/// Uses vLLM's global DP-rank port offset for both query clients and connector servers.
pub fn lookup_endpoint(host: &str, dp_rank: u32) -> String {
    format!("tcp://{host}:{}", LOOKUP_BASE_PORT + dp_rank)
}
pub const LOOKUP_ENDPOINT_ENV: &str = "FORETOKEN_SHARED_KV_LOOKUP_ENDPOINT";
pub const CONNECTOR_MODULE: &str = "foretoken_mooncake";
pub const MOONCAKE_CONNECTOR_MODULE: &str = CONNECTOR_MODULE;
pub const OFFLOADING_CONNECTOR_MODULE: &str =
    "vllm.distributed.kv_transfer.kv_connector.v1.offloading_connector";

#[derive(Clone)]
pub struct SharedKvLookup {
    model_group_id: String,
    scope_id: String,
    placement: KvPlacement,
    endpoints: Vec<String>,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ConnectorPrefixResponse {
    matched_tokens: Option<usize>,
    block_size: usize,
}

impl SharedKvLookup {
    /// Binds engine observations to the model-server identity published by the controller.
    pub fn new(
        model_group_id: String,
        scope_id: String,
        config: &crate::config::RuntimeConfig,
    ) -> Self {
        let placement = config
            .launch
            .shared_prefix_placement()
            .expect("shared KV lookup requires a connector-owned placement");
        let endpoints = (0..config.launch.parallelism.dp)
            .map(|rank| {
                let host = config.member.as_ref().map_or_else(
                    || crate::runtime_transport::LOOPBACK_HOST.to_string(),
                    |member| {
                        member.node_address(
                            rank * config.launch.node_count / config.launch.parallelism.dp,
                        )
                    },
                );
                lookup_endpoint(&host, rank as u32)
            })
            .collect();
        Self {
            model_group_id,
            scope_id,
            placement,
            endpoints,
        }
    }

    /// Queries the existing connector without retaining tokens, results, or a Store client.
    pub async fn lookup(&self, request: &KvSharedPrefixRequest) -> Option<KvSharedPrefixResponse> {
        let endpoint = self.endpoints.get(request.dp_rank as usize)?;
        let mut socket = zeromq::ReqSocket::new();
        socket.connect(endpoint).await.ok()?;
        socket
            .send(serde_json::to_vec(request).ok()?.into())
            .await
            .ok()?;
        let message = socket.recv().await.ok()?;
        if message.len() != 1 {
            return None;
        }
        let response: ConnectorPrefixResponse = serde_json::from_slice(message.get(0)?).ok()?;
        let matched_tokens = response.matched_tokens?;
        if response.block_size == 0
            || matched_tokens > request.prompt_token_ids.len()
            || !matched_tokens.is_multiple_of(response.block_size)
        {
            return None;
        }
        Some(KvSharedPrefixResponse {
            model_group_id: self.model_group_id.clone(),
            scope_id: self.scope_id.clone(),
            placement: self.placement,
            matched_tokens,
            block_size: response.block_size,
        })
    }
}
