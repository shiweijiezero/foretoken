// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Launches and supervises vLLM-Omni behind its opaque video HTTP adapter.

mod api;
mod launch;
mod serving;

/// Serves video requests for the Omni binary, owning the native process through shutdown.
pub async fn run() -> Result<(), Box<dyn std::error::Error>> {
    serving::Server::start().await?.serve().await
}
