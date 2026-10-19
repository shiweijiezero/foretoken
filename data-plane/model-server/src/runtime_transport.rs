// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Resolves execution-group transport ports around controller-declared listeners.

use crate::launch::{KvPlan, LaunchPlanV1};

/// Host shared by the managed engine and other Pod-local runtime transports.
pub const LOOPBACK_HOST: &str = "127.0.0.1";
pub(crate) const KV_EVENT_TOPIC: &str = "foretoken-kv-v1";

/// Shared ports used by every member of one distributed engine execution.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct DistributedPorts {
    pub master: u16,
    pub calibration: u16,
    pub calibration_probe: u16,
}

/// One resolved transport layout consumed by engine launch and runtime observers.
/// Members derive the same layout from their shared launch configuration, not local port probes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct RuntimePorts {
    pub handshake: u16,
    pub distributed: Option<DistributedPorts>,
    kv_events: u16,
    shared_kv_lookup: Option<u16>,
}

impl RuntimePorts {
    /// Selects non-overlapping private ranges without changing externally published endpoints.
    /// Actual listeners remain owned by their serving tasks or native engine processes.
    pub fn resolve(
        launch: &LaunchPlanV1,
        listen_port: u16,
        cache_port: Option<u16>,
    ) -> Result<Self, String> {
        let mut ports = PortRanges::default();
        ports.reserve(listen_port, 1)?;
        if let Some(port) = cache_port {
            ports.reserve(port, 1)?;
        }
        if matches!(
            &launch.kv,
            KvPlan::Pd { .. } | KvPlan::MultiConnector { .. }
        ) {
            ports.reserve_environment("VLLM_MOONCAKE_BOOTSTRAP_PORT", 1)?;
        }
        if launch
            .engine_args
            .get("load-format")
            .and_then(serde_json::Value::as_str)
            == Some("modelexpress")
        {
            let local_workers = launch.parallelism.tp
                * launch.parallelism.pp
                * launch.parallelism.pcp
                * launch.parallelism.dp
                / launch.node_count;
            ports.reserve_environment("MX_WORKER_GRPC_PORT", local_workers)?;
            ports.reserve_environment("MX_METADATA_PORT", local_workers)?;
        }

        let handshake = ports.select(29700, 1)?;
        let distributed = if launch.node_count > 1 {
            let calibration = ports.select(29600, 2)?;
            Some(DistributedPorts {
                calibration,
                calibration_probe: calibration + 1,
                master: ports.select(29800, 1)?,
            })
        } else {
            None
        };
        let kv_events = ports.select(30100, launch.parallelism.dp)?;
        let shared_kv_lookup = if launch.shared_prefix_lookup() {
            Some(ports.select(30200, launch.parallelism.dp)?)
        } else {
            None
        };
        Ok(Self {
            handshake,
            distributed,
            kv_events,
            shared_kv_lookup,
        })
    }

    /// Addresses one publisher's rank-offset channel on the group-local collector.
    pub fn kv_event_endpoint(&self, host: &str, dp_rank: u32) -> String {
        tcp_endpoint(host, u32::from(self.kv_events) + dp_rank)
    }

    /// Addresses the active Store connector using the native global DP-rank offset.
    pub fn lookup_endpoint(&self, host: &str, dp_rank: u32) -> String {
        let base = self
            .shared_kv_lookup
            .expect("shared lookup requires an allocated port range");
        tcp_endpoint(host, u32::from(base) + dp_rank)
    }
}

fn tcp_endpoint(host: &str, port: u32) -> String {
    if host.contains(':') && !host.starts_with('[') {
        format!("tcp://[{host}]:{port}")
    } else {
        format!("tcp://{host}:{port}")
    }
}

#[derive(Default)]
struct PortRanges(Vec<(u32, u32)>);

impl PortRanges {
    fn reserve(&mut self, start: u16, count: usize) -> Result<(), String> {
        if start == 0 {
            return Ok(());
        }
        if count == 0 || count > usize::from(u16::MAX) || u32::from(start) + count as u32 > 65536 {
            return Err("runtime port range exceeds the TCP port space".into());
        }
        self.0
            .push((u32::from(start), u32::from(start) + count as u32));
        Ok(())
    }

    fn reserve_environment(&mut self, name: &str, count: usize) -> Result<(), String> {
        match std::env::var(name) {
            Ok(value) => {
                let port = value
                    .parse::<u16>()
                    .map_err(|_| format!("{name} must be a TCP port"))?;
                self.reserve(port, count)
            }
            Err(std::env::VarError::NotPresent) => Ok(()),
            Err(error) => Err(format!("{name}: {error}")),
        }
    }

    // Private ports can move; stable published ports and complete rank ranges cannot overlap.
    fn select(&mut self, preferred: u16, count: usize) -> Result<u16, String> {
        if count == 0 || count > usize::from(u16::MAX) {
            return Err("runtime rank count exceeds the TCP port space".into());
        }
        let count = count as u32;
        let last = 65536 - count;
        let preferred = u32::from(preferred);
        for start in (preferred..=last).chain(1..preferred.min(last + 1)) {
            let end = start + count;
            if self
                .0
                .iter()
                .all(|&(used_start, used_end)| end <= used_start || start >= used_end)
            {
                self.0.push((start, end));
                return Ok(start as u16);
            }
        }
        Err("no contiguous port range remains for the runtime transport".into())
    }
}
