// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Maps capture configuration and worker operations to vLLM's native profiler contract.

use std::path::Path;

use async_trait::async_trait;

use super::VllmBackend;
use crate::profiling::{Config, Engine, EngineProfiler};

/// Render vLLM instrumentation without enabling recording before the supervisor requests it.
pub(super) fn engine_argument(config: &Config) -> String {
    if config.engine != Engine::Pytorch {
        // Native collectors own recording; do not install a competing PyTorch profiler.
        return "--profiler-config={}".into();
    }
    format!(
        "--profiler-config={}",
        serde_json::json!({
            "profiler": "torch",
            "torch_profiler_dir": config.profile_root().join(".staging"),
            "torch_profiler_use_gzip": false,
            "torch_profiler_dump_cuda_time_total": false,
            "torch_profiler_with_stack": false,
            "ignore_frontend": true,
        })
    )
}

#[async_trait]
impl EngineProfiler for VllmBackend {
    async fn set_pytorch_recording(&self, start: bool, prefix: &str) -> Result<(), String> {
        let guard = self.llm.read().await;
        let client = guard
            .as_ref()
            .ok_or("engine is unavailable")?
            .engine_core_client();
        let result = self
            .closing
            .run_until_cancelled(async {
                if start {
                    client.start_profile(Some(prefix)).await
                } else {
                    client.stop_profile(Some(prefix)).await
                }
            })
            .await
            .ok_or("engine is shutting down")?;
        result.map_err(|error| error.to_string())
    }

    async fn set_mctracer_recording(&self, start: bool, directory: &Path) -> Result<(), String> {
        let guard = self.llm.read().await;
        let client = guard
            .as_ref()
            .ok_or("engine is unavailable")?
            .engine_core_client();
        self.closing
            .run_until_cancelled(client.collective_rpc(
                "foretoken_mctracer",
                None,
                (start, directory.to_string_lossy()),
                std::collections::BTreeMap::<String, String>::new(),
            ))
            .await
            .ok_or("engine is shutting down")?
            .map(|_| ())
            .map_err(|error| error.to_string())
    }
}
