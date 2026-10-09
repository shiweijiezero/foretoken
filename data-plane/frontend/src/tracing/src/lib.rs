// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Foretoken's compatibility boundary for vLLM tracing initialization.

use tracing::level_filters::LevelFilter;
use tracing_subscriber::filter::Targets;
use tracing_subscriber::registry::Registry;
use tracing_subscriber::reload::Handle;
use tracing_subscriber::util::TryInitError;

pub use vllm_tracing::*;

/// Process-lifetime control of the frontend's default logging level.
pub struct LogControl {
    handle: Handle<Targets, Registry>,
}

impl LogControl {
    /// Installs upstream formatting once and retains the filter handle for snapshot publication.
    pub fn new(process_label: &str) -> Result<Self, TryInitError> {
        Ok(Self {
            handle: vllm_tracing::init_tracing_with_reload(process_label)?,
        })
    }

    /// Parses a candidate level without changing the active process filter.
    pub fn prepare(&self, level: &str) -> Result<LevelFilter, String> {
        level
            .parse()
            .map_err(|error| format!("invalid frontend log level {level:?}: {error}"))
    }

    /// Publishes a prepared default level while retaining explicit `RUST_LOG` target directives.
    pub fn apply(&self, level: LevelFilter) {
        self.handle
            .modify(|filter| *filter = filter.clone().with_default(level))
            .expect("process tracing subscriber must remain installed");
    }
}
