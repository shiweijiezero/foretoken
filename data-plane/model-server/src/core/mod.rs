// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral core of the group-local model server.
//!
//! This module owns the HTTP API, admission, and KV event projection. It never
//! imports an inference-engine crate; engines plug in through the `engine`
//! module's trait.

pub mod api;
pub mod config;
pub mod kv_events;
