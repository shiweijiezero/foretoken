// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Maps capture configuration and worker operations to vLLM's native profiler contract.

use std::path::Path;

use async_trait::async_trait;

use super::VllmBackend;
use crate::profiling::{Config, Engine, EngineProfiler, ExportResult};
use serde::Deserialize;

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
        let statuses =
            pytorch_operation(self, if start { "start" } else { "stop" }, prefix).await?;
        if !start && statuses.iter().any(|status| !status.recording_stopped) {
            return Err("PyTorch recording stop was not confirmed by every worker".into());
        }
        Ok(())
    }

    async fn finish_pytorch_export(&self, prefix: &str) -> Result<ExportResult, String> {
        loop {
            let statuses = pytorch_operation(self, "status", prefix).await?;
            if statuses.iter().any(|status| !status.recording_stopped) {
                return Err("PyTorch recording restarted before export completed".into());
            }
            if statuses
                .iter()
                .all(|status| status.exported || status.error.is_some())
            {
                let failures = statuses
                    .into_iter()
                    .filter_map(|status| status.error)
                    .collect::<Vec<_>>();
                return Ok(if failures.is_empty() {
                    ExportResult::Complete
                } else {
                    ExportResult::Failed(failures.join("; "))
                });
            }
            tokio::time::sleep(std::time::Duration::from_millis(250)).await;
        }
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

#[derive(Deserialize)]
struct CaptureStatus {
    recording_stopped: bool,
    exported: bool,
    error: Option<String>,
}

// Worker utilities own capture identity and preserve one status per native worker.
async fn pytorch_operation(
    backend: &VllmBackend,
    action: &str,
    prefix: &str,
) -> Result<Vec<CaptureStatus>, String> {
    let guard = backend.llm.read().await;
    let client = guard
        .as_ref()
        .ok_or("engine is unavailable")?
        .engine_core_client();
    let results = backend
        .closing
        .run_until_cancelled(client.collective_rpc(
            "foretoken_pytorch",
            None,
            (action, prefix),
            std::collections::BTreeMap::<String, String>::new(),
        ))
        .await
        .ok_or("engine is shutting down")?
        .map_err(|error| error.to_string())?;
    if results.is_empty() {
        return Err("PyTorch capture returned no worker statuses".into());
    }
    results
        .into_iter()
        .map(|value| rmpv::ext::from_value(value).map_err(|error| error.to_string()))
        .collect()
}
