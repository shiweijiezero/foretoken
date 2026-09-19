// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Transport addresses owned by the model-server execution group.

/// Host shared by the managed engine and other Pod-local runtime transports.
pub const LOOPBACK_HOST: &str = "127.0.0.1";
pub(crate) const KV_EVENT_TOPIC: &str = "foretoken-kv-v1";
const KV_EVENT_TCP_BASE_PORT: u32 = 30100;

/// Addresses one DP publisher's channel on the group-local collector.
/// vLLM applies the same rank offset to the configured base endpoint.
pub(crate) fn kv_event_endpoint(host: &str, dp_rank: u32) -> String {
    format!("tcp://{host}:{}", KV_EVENT_TCP_BASE_PORT + dp_rank)
}
