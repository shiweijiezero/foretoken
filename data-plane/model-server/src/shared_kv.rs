// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Reads shared prefix observations from the active engine's Pod-local connector.

use foretoken_model_protocol::{KvSharedPrefixRequest, KvSharedPrefixResponse};
use serde::Deserialize;
use zeromq::prelude::{Socket, SocketRecv, SocketSend};

pub const LOOKUP_ENDPOINT: &str = "ipc:///tmp/foretoken-shared-kv.sock";
pub const LOOKUP_ENDPOINT_ENV: &str = "FORETOKEN_SHARED_KV_LOOKUP_ENDPOINT";
pub const CONNECTOR_MODULE: &str = "foretoken_mooncake";

#[derive(Clone)]
pub struct SharedKvLookup {
    model_group_id: String,
    scope_id: String,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase")]
struct ConnectorPrefixResponse {
    matched_tokens: Option<usize>,
    block_size: usize,
}

impl SharedKvLookup {
    /// Binds engine observations to the model-server identity published by the controller.
    pub fn new(model_group_id: String, scope_id: String) -> Self {
        Self {
            model_group_id,
            scope_id,
        }
    }

    /// Queries the existing connector without retaining tokens, results, or a Store client.
    pub async fn lookup(&self, request: &KvSharedPrefixRequest) -> Option<KvSharedPrefixResponse> {
        if request.dp_rank != 0 {
            return None;
        }
        let mut socket = zeromq::ReqSocket::new();
        socket.connect(LOOKUP_ENDPOINT).await.ok()?;
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
            matched_tokens,
            block_size: response.block_size,
        })
    }
}
