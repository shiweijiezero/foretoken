// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Group-local typed streaming API backed by one vLLM EngineCore instance.

pub mod api;
pub mod backend;
mod backend_telemetry;
pub mod config;
pub mod kv_event_adapter;
pub mod launch;
