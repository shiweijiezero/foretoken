// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Group-local typed streaming API backed by one vLLM EngineCore instance.

pub mod core;
pub mod engine;
#[doc(hidden)]
pub mod runtime_transport;
