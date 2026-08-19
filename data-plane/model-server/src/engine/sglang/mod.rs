// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! SGLang engine adapter.
//!
//! SGLang has no Rust engine client, so this adapter spawns the server as a
//! loopback child process and talks to its native HTTP `/generate` endpoint.
//! The engine-neutral core only sees the [`crate::engine::Engine`] trait.

mod backend;
mod launch_plan;
mod process;

pub use backend::SglangBackend;
pub use launch_plan::SglangLaunchPlan;
pub use process::SglangProcess;
