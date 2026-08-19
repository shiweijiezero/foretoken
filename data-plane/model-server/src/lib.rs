// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Group-local typed streaming API backed by one inference engine.

#[cfg(all(feature = "backend-vllm", feature = "backend-sglang"))]
compile_error!("exactly one backend feature (vllm or sglang) may be enabled");

pub mod core;
pub mod engine;
#[doc(hidden)]
pub mod runtime_transport;
