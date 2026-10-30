// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Private versioned launch contract and the sole vLLM argv renderer.

use std::collections::BTreeMap;
use std::process::Command;
use std::time::Duration;

use serde::{Deserialize, Serialize};
use serde_json::json;
use vllm_managed_engine::ManagedEngineConfig;

use foretoken_artifacts::{ModelArtifacts, ModelSource};
use foretoken_model_protocol::{
    KvCacheLocality, KvConnector, KvPlacement, KvStorageTier, RuntimeEcTransferMetadata,
    RuntimeKvTransferMetadata,
};

use crate::backend::vllm::transport::{KV_EVENT_TOPIC, LOOPBACK_HOST, RuntimePorts};
use crate::engine::{Lifecycle, append_engine_arg, python_executable};

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LaunchPlanV1 {
    pub version: u8,
    /// Kubernetes nodes participating in this complete execution group.
    #[serde(rename = "nodeCount")]
    pub node_count: usize,
    pub artifacts: ModelArtifacts,
    pub parallelism: Parallelism,
    pub kv: KvPlan,
    #[serde(default)]
    pub ec: EcTransferPlan,
    #[serde(default)]
    pub dt: Option<DraftTargetPlan>,
    pub lifecycle: Lifecycle,
    #[serde(default)]
    pub profiling: crate::profiling::Preparation,
    #[serde(rename = "internalGenerateRequestBodyLimitBytes")]
    pub internal_generate_request_body_limit_bytes: usize,
    #[serde(default, rename = "engineArgs")]
    pub engine_args: BTreeMap<String, serde_json::Value>,
}

/// Controller-resolved speculative execution and its optional proposal checkpoint.
#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
pub struct DraftTargetPlan {
    pub role: SpeculationRole,
    pub draft_artifacts: Option<ModelArtifacts>,
    #[serde(default)]
    pub draft_endpoints: Vec<String>,
    pub draft_endpoint: Option<String>,
    pub protocol: DraftTargetProtocol,
}

/// Controller-selected transport shared by a connected Draft/Target cohort.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum DraftTargetProtocol {
    Tcp,
    Rdma,
    Nccl,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum SpeculationRole {
    Draft,
    Target,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Parallelism {
    pub tp: usize,
    pub pp: usize,
    pub dp: usize,
    pub pcp: usize,
    pub dcp: usize,
    pub ep: Option<ExpertParallelism>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ExpertParallelism {
    #[serde(default)]
    pub backend: String,
    pub eplb: bool,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum KvPlan {
    #[serde(rename = "none")]
    None { events: bool },
    #[serde(rename = "pd")]
    Pd {
        #[serde(default = "KvPlan::legacy_connector")]
        connector: KvConnector,
        role: KvRole,
        protocol: TransferProtocol,
        #[serde(default, rename = "deviceName")]
        device_name: String,
        events: bool,
    },
    #[serde(rename = "cpuOffload")]
    CpuOffload {
        #[serde(rename = "cpuBytes")]
        cpu_bytes: i64,
        events: bool,
    },
    #[serde(rename = "filesystemOffload")]
    FilesystemOffload {
        #[serde(rename = "cpuBytes")]
        cpu_bytes: i64,
        /// Writable directory mounted by the workload projection for this Group.
        #[serde(rename = "storagePath")]
        storage_path: String,
        events: bool,
    },
    #[serde(rename = "mooncakeStore")]
    MooncakeStore { role: KvRole, events: bool },
    #[serde(rename = "multiConnector")]
    MultiConnector {
        #[serde(default = "KvPlan::legacy_connector")]
        connector: KvConnector,
        role: KvRole,
        protocol: TransferProtocol,
        #[serde(default, rename = "deviceName")]
        device_name: String,
        events: bool,
    },
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum KvRole {
    KvBoth,
    KvProducer,
    KvConsumer,
}

/// Selects the native network transport for P/D and Draft/Target tensor exchange.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub enum TransferProtocol {
    #[serde(rename = "rdma")]
    Rdma,
    #[serde(rename = "tcp")]
    Tcp,
}

/// Controller-owned EC transfer configuration for one model-server.
///
/// The local vLLM source release exposes `ECExampleConnector` as its reference
/// disaggregated-encoder path. The workload projection mounts one writable, platform-owned
/// ReadWriteMany volume at `sharedStoragePath` for both roles; clients cannot select it.
#[derive(Debug, Clone, PartialEq, Eq, Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EcTransferPlan {
    #[serde(default, rename = "profileName")]
    profile_name: String,
    #[serde(default, rename = "profileRevision")]
    profile_revision: String,
    #[serde(default)]
    connector: String,
    #[serde(default)]
    role: Option<EcRole>,
    #[serde(default, rename = "sharedStoragePath")]
    shared_storage_path: String,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum EcRole {
    Producer,
    Consumer,
}

impl LaunchPlanV1 {
    /// Reads the controller's vLLM launch selection for serving or model preparation.
    pub fn from_env() -> Result<Self, String> {
        const NAME: &str = "FORETOKEN_VLLM_LAUNCH_PLAN";
        let value = std::env::var(NAME).map_err(|error| format!("{NAME}: {error}"))?;
        Self::parse(&value)
    }

    /// Decodes the controller-projected launch plan used by model-server startup.
    ///
    /// Returns an owned, validated plan or a configuration error without retaining the input.
    pub fn parse(input: &str) -> Result<Self, String> {
        let plan: Self = serde_json::from_str(input)
            .map_err(|error| format!("invalid FORETOKEN_VLLM_LAUNCH_PLAN: {error}"))?;
        plan.validate()?;
        Ok(plan)
    }

    /// Verifies constraints required by the engine launcher and argv renderer.
    ///
    /// Callers retain the plan; success publishes no state, while failure reports the invalid field.
    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1 {
            return Err("launch plan version must be 1".into());
        }
        if self.node_count == 0 {
            return Err("launch plan requires a positive node count".into());
        }
        self.artifacts.validate()?;
        if let Some(dt) = &self.dt {
            if let Some(draft) = &dt.draft_artifacts {
                draft.validate()?;
                if draft.source != self.artifacts.source {
                    return Err("Draft and Target must use the same model source".into());
                }
            }
            if dt.role == SpeculationRole::Target {
                if dt.draft_endpoints.iter().any(String::is_empty) {
                    return Err("Target launch contains an empty Draft endpoint".into());
                }
                if dt.draft_endpoints.is_empty()
                    && dt.draft_endpoint.as_ref().is_none_or(String::is_empty)
                {
                    return Err("Target launch requires a Draft endpoint pool".into());
                }
            }
        }
        let p = &self.parallelism;
        if p.tp == 0 || p.pp == 0 || p.dp == 0 || p.pcp == 0 || p.dcp == 0 {
            return Err("launch plan topology values must be positive".into());
        }
        if !(p.tp * p.pp * p.pcp * p.dp).is_multiple_of(self.node_count) {
            return Err("worker count must divide evenly across model nodes".into());
        }
        if let Some(ep) = &p.ep
            && ep.eplb
            && p.tp * p.pcp * p.dp == 1
        {
            return Err("EPLB requires more than one expert-parallel rank".into());
        }
        if self.lifecycle.startup_seconds == 0 || self.lifecycle.drain_seconds == 0 {
            return Err("launch plan lifecycle seconds must be positive".into());
        }
        match &self.kv {
            KvPlan::CpuOffload { cpu_bytes, .. } | KvPlan::FilesystemOffload { cpu_bytes, .. }
                if *cpu_bytes <= 0 =>
            {
                return Err("KV offload cpuBytes must be positive".into());
            }
            KvPlan::FilesystemOffload { storage_path, .. }
                if !is_absolute_storage_path(storage_path) =>
            {
                return Err(
                    "filesystemOffload storagePath must be an absolute mounted directory".into(),
                );
            }
            KvPlan::Pd {
                role: KvRole::KvBoth,
                ..
            }
            | KvPlan::MultiConnector {
                role: KvRole::KvBoth,
                ..
            } => return Err("P/D KV plans require a producer or consumer role".into()),
            KvPlan::MooncakeStore {
                role: KvRole::KvProducer,
                ..
            } => return Err("Mooncake Store cannot be producer-only".into()),
            _ => {}
        }
        self.ec.validate()
    }

    /// Returns the EngineCore connection deadline consumed during model-server startup.
    ///
    /// The duration is derived from the retained controller-owned lifecycle plan.
    pub fn startup_timeout(&self) -> Duration {
        Duration::from_secs(self.lifecycle.startup_seconds)
    }

    /// Returns the shutdown drain deadline consumed by HTTP and managed-engine teardown.
    ///
    /// The duration is derived from the retained controller-owned lifecycle plan.
    pub fn drain_timeout(&self) -> Duration {
        Duration::from_secs(self.lifecycle.drain_seconds)
    }

    /// Builds engine startup and version-derived KV settings for the runtime supervisor.
    /// The connector's effective settings are observed after engine readiness.
    pub fn managed_engine(
        &self,
        handshake_port: u16,
        member: Option<&crate::backend::vllm::config::MemberContext>,
        retained_prefixes: bool,
        ports: &RuntimePorts,
    ) -> Result<(ManagedEngineConfig, Option<RuntimeKvTransferMetadata>), String> {
        let kv_transfer =
            self.kv
                .runtime_metadata(retained_prefixes, self.node_count, &self.parallelism);
        let mut config = ManagedEngineConfig {
            python: python_executable(),
            model: self.artifacts.model.clone(),
            handshake_host: LOOPBACK_HOST.into(),
            handshake_port,
            data_parallel_size: self.parallelism.dp,
            python_args: self.render_vllm_args(member, kv_transfer.as_ref(), ports)?,
        };
        if let Some(member) = member {
            config.handshake_host = member.leader_address.clone();
            config.python_args.extend([
                format!("--nnodes={}", self.node_count),
                format!("--node-rank={}", member.index),
                format!("--master-addr={}", member.leader_address),
                format!(
                    "--master-port={}",
                    ports
                        .distributed
                        .expect("distributed member requires transport ports")
                        .master
                ),
            ]);
        }
        Ok((config, kv_transfer))
    }

    /// Builds the startup command, including network calibration for distributed members.
    ///
    /// The launcher execs the original engine, so its probes share the managed process group.
    pub fn engine_command(
        &self,
        engine: &ManagedEngineConfig,
        member: Option<&crate::backend::vllm::config::MemberContext>,
        ports: &RuntimePorts,
        startup_remaining: Duration,
    ) -> Command {
        let command = if self
            .dt
            .as_ref()
            .is_some_and(|dt| dt.role == SpeculationRole::Draft)
        {
            let mut command = Command::new(&engine.python);
            command
                .args(["-m", "foretoken_dt", "--model"])
                .arg(&engine.model)
                .arg("--data-parallel-address")
                .arg(&engine.handshake_host)
                .arg("--data-parallel-rpc-port")
                .arg(engine.handshake_port.to_string())
                .arg("--data-parallel-size")
                .arg(engine.data_parallel_size.to_string())
                .args(&engine.python_args);
            command
        } else {
            engine.to_command()
        };
        let mut command = if let Some(member) = member {
            let distributed = ports
                .distributed
                .expect("distributed launcher requires transport ports");
            let mut launcher = Command::new(command.get_program());
            launcher
                .args(["-m", "mccl", "launch"])
                .arg(
                    json!({
                        "member_index": member.index,
                        "member_count": self.node_count,
                        "leader": member.leader_address,
                        "port": distributed.calibration,
                        "probe_port": distributed.calibration_probe,
                        "startup_seconds": startup_remaining.as_secs_f64(),
                    })
                    .to_string(),
                )
                .args(command.get_args());
            launcher
        } else {
            command
        };
        if let Some(dt) = &self.dt
            && dt.role == SpeculationRole::Target
        {
            // Peer bindings change across deployments without changing the compiled graph.
            let endpoint = dt
                .draft_endpoint
                .as_deref()
                .or_else(|| dt.draft_endpoints.first().map(String::as_str))
                .expect("validated Target endpoint");
            let endpoints = if dt.draft_endpoints.is_empty() {
                serde_json::to_string(&vec![endpoint]).expect("Draft endpoint pool is serializable")
            } else {
                serde_json::to_string(&dt.draft_endpoints)
                    .expect("Draft endpoint pool is serializable")
            };
            command.env("FORETOKEN_DT_DRAFT_ENDPOINT", endpoint);
            command.env("FORETOKEN_DT_DRAFT_ENDPOINTS", endpoints);
        }
        match &self.kv {
            KvPlan::Pd {
                connector: KvConnector::Mooncake,
                protocol,
                ..
            }
            | KvPlan::MultiConnector {
                connector: KvConnector::Mooncake,
                protocol,
                ..
            } => {
                // Mooncake tests presence, so an inherited opposite flag must be removed.
                command
                    .env_remove("MC_FORCE_HCA")
                    .env_remove("MC_FORCE_TCP");
                command.env(
                    match protocol {
                        TransferProtocol::Rdma => "MC_FORCE_HCA",
                        TransferProtocol::Tcp => "MC_FORCE_TCP",
                    },
                    "1",
                );
            }
            KvPlan::Pd {
                connector: KvConnector::Nixl,
                protocol,
                device_name,
                ..
            } => {
                // Protocol v2 preserves device memory types through software RMA.
                command.env("UCX_PROTO_ENABLE", "y");
                // Exclude the other network family without filtering native memory transports.
                command.env(
                    "UCX_TLS",
                    match protocol {
                        TransferProtocol::Tcp => "^ib",
                        TransferProtocol::Rdma => "^tcp",
                    },
                );
                if *protocol == TransferProtocol::Rdma && !device_name.is_empty() {
                    command.env("UCX_NET_DEVICES", device_name);
                }
            }
            _ => {}
        }
        command
    }

    /// Renders the owned vLLM arguments consumed by the managed-engine child process.
    ///
    /// Validation runs before rendering; the returned vector does not borrow the launch plan.
    fn render_vllm_args(
        &self,
        member: Option<&crate::backend::vllm::config::MemberContext>,
        kv_transfer: Option<&RuntimeKvTransferMetadata>,
        ports: &RuntimePorts,
    ) -> Result<Vec<String>, String> {
        self.validate()?;
        let p = &self.parallelism;
        let mut args = Vec::new();
        if self.artifacts.source != ModelSource::Local {
            args.push(format!("--revision={}", self.artifacts.revision));
        }
        args.push(format!("--tokenizer={}", self.artifacts.tokenizer));
        if self.artifacts.source != ModelSource::Local {
            args.push(format!(
                "--tokenizer-revision={}",
                self.artifacts.tokenizer_revision
            ));
        }
        args.extend([
            format!("--tensor-parallel-size={}", p.tp),
            format!("--pipeline-parallel-size={}", p.pp),
            format!("--prefill-context-parallel-size={}", p.pcp),
            format!("--decode-context-parallel-size={}", p.dcp),
        ]);
        if let Some(ep) = &p.ep {
            args.push("--enable-expert-parallel".into());
            if !ep.backend.is_empty() {
                args.push(format!("--all2all-backend={}", ep.backend));
            }
            if ep.eplb {
                args.push("--enable-eplb".into());
            }
        }
        let mut engine_args = self.engine_args.clone();
        if let Some(dt) = &self.dt {
            let settings = json!({"role": dt.role, "protocol": dt.protocol});
            let additional = engine_args
                .entry("additional-config".into())
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .ok_or("engineArgs.additional-config must be an object")?;
            additional.insert("foretoken_dt".into(), settings);
            if dt.role == SpeculationRole::Target {
                engine_args.insert(
                    "worker-extension-cls".into(),
                    json!("foretoken_dt.worker.WorkerExtension"),
                );
            }
        }
        // The controller has already normalized native option names.
        // Keep argument values intact: this command never goes through a shell.
        for (name, value) in &engine_args {
            append_engine_arg(&mut args, name, value)?;
        }
        // Retained Group arguments may predate controller-owned shutdown budgets.
        args.push(format!(
            "--shutdown-timeout={}",
            self.lifecycle.drain_seconds
        ));
        if self.kv.events() {
            args.push(format!("--kv-events-config={}", json!({"publisher":"zmq","endpoint":ports.kv_event_endpoint(member.map_or(LOOPBACK_HOST, |member| member.leader_address.as_str()), 0),"topic":KV_EVENT_TOPIC,"enable_kv_cache_events":true,"hwm":4096,"max_queue_size":4096})));
        }
        if let Some(config) = self.kv.transfer_config(
            self.shared_prefix_lookup(),
            member.map(|member| member.model_group_uid.as_str()),
            kv_transfer,
        ) {
            args.push(format!("--kv-transfer-config={config}"));
        }
        if let Some(config) = self.ec.transfer_config() {
            args.push(format!("--ec-transfer-config={config}"));
        }
        Ok(args)
    }

    /// Returns the connector-owned placement exposed by live prefix observation.
    pub fn shared_prefix_placement(&self) -> Option<KvPlacement> {
        match self.kv {
            KvPlan::MooncakeStore { .. } | KvPlan::MultiConnector { .. } => Some(KvPlacement {
                tier: KvStorageTier::External,
                locality: KvCacheLocality::Remote,
            }),
            _ => None,
        }
    }

    /// Reports whether the selected connector exposes live shared-prefix observations.
    pub fn shared_prefix_lookup(&self) -> bool {
        self.shared_prefix_placement().is_some()
    }
}

impl KvRole {
    fn as_str(self) -> &'static str {
        match self {
            Self::KvBoth => "kv_both",
            Self::KvProducer => "kv_producer",
            Self::KvConsumer => "kv_consumer",
        }
    }
}

impl TransferProtocol {
    fn as_str(self) -> &'static str {
        match self {
            Self::Rdma => "rdma",
            Self::Tcp => "tcp",
        }
    }
}

impl EcRole {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Producer => "ec_producer",
            Self::Consumer => "ec_consumer",
        }
    }
}

impl EcTransferPlan {
    /// Reports whether this launch plan configures an EC connector.
    pub fn enabled(&self) -> bool {
        !self.connector.is_empty()
    }

    /// Builds EC transfer identity published to runtime metadata consumers.
    ///
    /// The response owns cloned plan values and is absent when no valid role is configured.
    pub fn runtime_metadata(&self) -> Option<RuntimeEcTransferMetadata> {
        Some(RuntimeEcTransferMetadata {
            role: self.role?.as_str().into(),
            profile: self.profile_name.clone(),
            connector: self.connector.clone(),
        })
    }

    // Reject incomplete controller projections before they reach a child process, where EC connector
    // failures would otherwise appear only after launch.
    fn validate(&self) -> Result<(), String> {
        if !self.enabled() {
            if self.profile_name.is_empty()
                && self.profile_revision.is_empty()
                && self.role.is_none()
                && self.shared_storage_path.is_empty()
            {
                return Ok(());
            }
            return Err("EC transfer config must be either absent or complete".into());
        }
        if self.connector != "ECExampleConnector"
            || self.profile_name.is_empty()
            || self.profile_revision.is_empty()
            || self.role.is_none()
            || !is_absolute_storage_path(&self.shared_storage_path)
        {
            return Err("EC transfer config is incomplete or unsupported".into());
        }
        Ok(())
    }

    // Translate the controller-owned EC fields into vLLM's child-process JSON boundary. The
    // temporary value is consumed by argv rendering and never published by this module.
    fn transfer_config(&self) -> Option<serde_json::Value> {
        let role = self.role?;
        Some(json!({
            "ec_connector": "SharedStorageConnector",
            "ec_connector_module_path": "connectors.ec",
            "ec_role": role.as_str(),
            "ec_connector_extra_config": {
                "shared_storage_path": self.shared_storage_path,
            },
        }))
    }
}

impl KvPlan {
    // A v1 P/D plan without a connector field denotes Mooncake.
    fn legacy_connector() -> KvConnector {
        KvConnector::Mooncake
    }

    fn events(&self) -> bool {
        match self {
            Self::None { events }
            | Self::Pd { events, .. }
            | Self::CpuOffload { events, .. }
            | Self::FilesystemOffload { events, .. }
            | Self::MooncakeStore { events, .. }
            | Self::MultiConnector { events, .. } => *events,
        }
    }

    // Resolve version-sensitive choices once; argv and runtime metadata consume this result.
    fn runtime_metadata(
        &self,
        retained_prefixes: bool,
        node_count: usize,
        parallelism: &Parallelism,
    ) -> Option<RuntimeKvTransferMetadata> {
        let connector = match self {
            Self::Pd { connector, .. } | Self::MultiConnector { connector, .. } => *connector,
            _ => return None,
        };
        Some(RuntimeKvTransferMetadata {
            connector,
            bidirectional_kv_xfer: matches!(self, Self::Pd { .. })
                && retained_prefixes
                && node_count == 1
                && parallelism.pp == 1
                && parallelism.pcp == 1
                && parallelism.dcp == 1,
        })
    }

    // Map the controller plan and resolved engine settings to native connector arguments.
    fn transfer_config(
        &self,
        shared_prefix_lookup: bool,
        model_group_uid: Option<&str>,
        kv_transfer: Option<&RuntimeKvTransferMetadata>,
    ) -> Option<serde_json::Value> {
        let pd = |role: KvRole, protocol: TransferProtocol, device_name: &str| {
            let settings = kv_transfer.expect("P/D settings are resolved before argv rendering");
            let module = match settings.connector {
                KvConnector::Mooncake => "connectors.mooncake.connector",
                KvConnector::Nixl => "connectors.nixl.connector",
            };
            let mut config = json!({
                "kv_connector": settings.connector.as_str(),
                "kv_role": role.as_str(),
                "kv_connector_extra_config": {},
            });
            if settings.connector == KvConnector::Mooncake {
                config["kv_connector_extra_config"] = json!({
                    "mooncake_protocol": protocol.as_str(),
                    "device_name": device_name,
                });
            }
            if settings.bidirectional_kv_xfer {
                config["kv_connector_module_path"] = json!(module);
                config["kv_connector_extra_config"]["bidirectional_kv_xfer"] = json!(true);
                if role == KvRole::KvProducer {
                    config["kv_load_failure_policy"] = json!("recompute");
                }
            }
            config
        };
        let store = |role: KvRole| {
            let mut config = json!({"kv_connector":"MooncakeStoreConnector","kv_role":role.as_str(),"kv_load_failure_policy":"recompute"});
            if shared_prefix_lookup {
                config["kv_connector_module_path"] =
                    json!(crate::backend::vllm::prefix_lookup::MOONCAKE_STORE_CONNECTOR_MODULE);
            }
            config
        };
        let mut config = match self {
            Self::None { .. } => None,
            Self::Pd {
                role,
                protocol,
                device_name,
                ..
            } => Some(pd(*role, *protocol, device_name)),
            Self::CpuOffload {
                cpu_bytes, events, ..
            } => Some(
                json!({"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_connector_extra_config":{"cpu_bytes_to_use":cpu_bytes,"spec_name":"CPUOffloadingSpec","self_describing_kv_events":events}}),
            ),
            Self::FilesystemOffload {
                cpu_bytes,
                storage_path,
                events,
            } => Some(
                json!({"kv_connector":"OffloadingConnector","kv_connector_module_path":crate::backend::vllm::prefix_lookup::OFFLOADING_CONNECTOR_MODULE,"kv_role":"kv_both","kv_connector_extra_config":{"cpu_bytes_to_use":cpu_bytes,"spec_name":"TieringOffloadingSpec","secondary_tiers":[{"type":"fs","root_dir":storage_path,"enable_kv_events":events}]}}),
            ),
            Self::MooncakeStore { role, .. } => Some(store(*role)),
            Self::MultiConnector {
                role,
                protocol,
                device_name,
                ..
            } => {
                let store_role = if *role == KvRole::KvConsumer {
                    KvRole::KvConsumer
                } else {
                    KvRole::KvBoth
                };
                Some(
                    json!({"kv_connector":"MultiConnector","kv_role":role.as_str(),"kv_load_failure_policy":"recompute","kv_connector_extra_config":{"connectors":[pd(*role, *protocol, device_name), store(store_role)]}}),
                )
            }
        }?;
        // NIXL peers and retained Mooncake sources need per-launch identities.
        // Preserve Group identity for older distributed Mooncake.
        if !kv_transfer.is_some_and(|transfer| {
            transfer.connector == KvConnector::Nixl || transfer.bidirectional_kv_xfer
        }) && let Some(uid) = model_group_uid
        {
            config["engine_id"] = json!(uid);
        }
        Some(config)
    }
}

fn is_absolute_storage_path(value: &str) -> bool {
    value.starts_with('/') && value != "/" && !value.contains(char::is_whitespace)
}
