// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Group-local typed streaming API backed by one vLLM EngineCore instance.

pub mod api;
pub mod backend;
pub mod cache_gc;
pub mod config;
pub mod instance_admission;
mod kubernetes;
pub mod kv_event_adapter;
pub mod launch;
pub mod managed_engine;
pub mod preparation;
pub mod profiling;
pub mod runtime_cache;
#[doc(hidden)]
pub mod runtime_transport;
pub mod shared_kv;

/// Waits for process termination requested by the serving or preparation supervisor.
/// Callers retain responsibility for draining their tasks and child processes.
pub async fn shutdown_signal() -> std::io::Result<()> {
    #[cfg(unix)]
    {
        use tokio::signal::unix::{SignalKind, signal};
        let mut terminate = signal(SignalKind::terminate())?;
        tokio::select! {
            result = tokio::signal::ctrl_c() => result,
            _ = terminate.recv() => Ok(()),
        }
    }
    #[cfg(not(unix))]
    tokio::signal::ctrl_c().await
}
