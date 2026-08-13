// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Builds immutable component inventory and dynamic execution facades from snapshots.

mod build;
mod registry;
mod route_target_stats;
mod snapshot;

pub use registry::{BackendRegistry, RouteTable};
pub use snapshot::{
    ModelIdentity, ServingSnapshot, SnapshotEpdComponent, SnapshotEpdPipelineScope, SnapshotError,
    SnapshotGroup, SnapshotModel, SnapshotPdComponent, SnapshotPdPipelineScope,
};

use foretoken_kv_indexer::KvRuntimeConfig;

/// Coupled routing and KV runtime projection from one snapshot.
pub struct BackendRegistryBuild {
    pub registry: BackendRegistry,
    pub kv_runtime_config: KvRuntimeConfig,
}

impl BackendRegistryBuild {
    pub fn from_json(bytes: &[u8]) -> Result<Self, SnapshotError> {
        Self::from_snapshot(serde_json::from_slice(bytes).map_err(SnapshotError::Parse)?)
    }

    pub fn from_snapshot(snapshot: ServingSnapshot) -> Result<Self, SnapshotError> {
        let kv_runtime_config = build::kv_runtime_config(&snapshot)?;
        let registry = BackendRegistry::from_snapshot(snapshot)?;
        Ok(Self {
            registry,
            kv_runtime_config,
        })
    }
}
