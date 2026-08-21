// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared addresses for model-server processes that communicate within one Pod.

/// Host shared by the managed engine and other Pod-local runtime transports.
pub const LOOPBACK_HOST: &str = "127.0.0.1";
pub(crate) const KV_EVENT_ENDPOINT: &str = "ipc:///tmp/foretoken-kv-events.sock";
pub(crate) const KV_EVENT_TOPIC: &str = "foretoken-kv-v1";
