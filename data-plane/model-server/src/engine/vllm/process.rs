// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Managed vLLM `EngineCore` process lifecycle.
//!
//! Owns spawning the engine child, connecting the loopback client, and
//! observing child/health state. The HTTP server and drain orchestration stay
//! in the binary entrypoint.

use std::time::Duration;

use vllm_engine_core_client::{EngineCoreClient, EngineCoreClientConfig, TransportMode};
use vllm_managed_engine::{ManagedEngineHandle, allocate_handshake_port};

use super::launch_plan::LaunchPlanV1;
use crate::runtime_transport::LOOPBACK_HOST;

/// A spawned vLLM engine child plus its connected client.
pub struct VllmProcess {
    pub engine: ManagedEngineHandle,
    client: Option<EngineCoreClient>,
}

impl VllmProcess {
    /// Spawns the managed engine child and connects the loopback client.
    pub async fn spawn(plan: &LaunchPlanV1) -> Result<Self, Box<dyn std::error::Error>> {
        let handshake_port = allocate_handshake_port(LOOPBACK_HOST)?;
        let engine = ManagedEngineHandle::spawn(
            plan.managed_engine(handshake_port)
                .map_err(std::io::Error::other)?,
        )
        .await?;

        let client_config = EngineCoreClientConfig {
            transport_mode: TransportMode::HandshakeOwner {
                handshake_address: format!("tcp://{LOOPBACK_HOST}:{handshake_port}"),
                advertised_host: LOOPBACK_HOST.into(),
                engine_count: plan.parallelism.dp,
                ready_timeout: plan.startup_timeout(),
                local_input_address: None,
                local_output_address: None,
            },
            coordinator_mode: None,
            model_name: plan.artifacts.model.clone(),
            client_index: 0,
        };
        let client = tokio::select! {
            result = EngineCoreClient::connect(client_config) => result.map_err(|error| std::io::Error::other(format!("could not connect to EngineCore: {error}"))),
            status = engine.wait_for_exit() => Err(std::io::Error::other(format!("managed EngineCore exited during startup: {status}"))),
        };
        let client = match client {
            Ok(client) => client,
            Err(error) => {
                let _ = engine.shutdown(plan.drain_timeout()).await;
                return Err(error.into());
            }
        };
        Ok(Self {
            engine,
            client: Some(client),
        })
    }

    /// Borrows the connected client for metadata reads.
    pub fn client(&self) -> &EngineCoreClient {
        self.client
            .as_ref()
            .expect("client is present until consumed by into_client")
    }

    /// Consumes the connected client, handing ownership to the backend.
    pub fn take_client(&mut self) -> EngineCoreClient {
        self.client
            .take()
            .expect("client must not be consumed twice")
    }

    /// Maximum concurrent sequences summed across ready engine responses.
    pub fn max_concurrent_requests(&self) -> Result<u64, std::io::Error> {
        self.client()
            .ready_responses()
            .into_iter()
            .try_fold(0_u64, |total, ready| total.checked_add(ready.max_num_seqs))
            .ok_or_else(|| std::io::Error::other("EngineCore max_num_seqs sum overflowed"))
    }

    /// Shuts the engine child down within the remaining drain budget.
    pub async fn shutdown(&self, remaining: Duration) -> Result<(), std::io::Error> {
        self.engine
            .shutdown(remaining)
            .await
            .map_err(std::io::Error::other)
    }
}
