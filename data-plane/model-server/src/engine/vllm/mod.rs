// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! vLLM engine adapter.
//!
//! This submodule owns everything vLLM-specific: the launch plan, the
//! `EngineCore` process lifecycle, the backend implementation, telemetry reads,
//! and the neutral-protocol conversions. The engine-neutral core only sees the
//! [`crate::engine::Engine`] trait.

mod backend;
pub mod conversion;
mod launch_plan;
mod process;
mod telemetry;

pub use backend::VllmBackend;
pub use launch_plan::LaunchPlanV1;
pub use process::VllmProcess;
