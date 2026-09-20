// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Private versioned launch contract and the sole vLLM argv renderer.

use std::collections::BTreeMap;
use std::path::Path;
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;
use vllm_managed_engine::ManagedEngineConfig;

use foretoken_artifacts::ModelSource;
use foretoken_model_protocol::{
    KvCacheLocality, KvPlacement, KvStorageTier, RuntimeEcTransferMetadata,
};

use crate::runtime_transport::{KV_EVENT_TOPIC, LOOPBACK_HOST, kv_event_endpoint};

const VLLM_PYTHON_ENV: &str = "FORETOKEN_VLLM_PYTHON";
const VLLM_USE_MODELSCOPE_ENV: &str = "VLLM_USE_MODELSCOPE";
const DEFAULT_VLLM_PYTHON: &str = "python";

#[derive(Debug, Clone, PartialEq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LaunchPlanV1 {
    pub version: u8,
    /// Kubernetes nodes participating in this complete execution group.
    #[serde(rename = "nodeCount")]
    pub node_count: usize,
    pub artifacts: Artifacts,
    pub parallelism: Parallelism,
    pub kv: KvPlan,
    #[serde(default)]
    pub ec: EcTransferPlan,
    pub lifecycle: Lifecycle,
    #[serde(default)]
    pub profiling: crate::profiling::Preparation,
    #[serde(rename = "internalGenerateRequestBodyLimitBytes")]
    pub internal_generate_request_body_limit_bytes: usize,
    #[serde(default, rename = "engineArgs")]
    pub engine_args: BTreeMap<String, serde_json::Value>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Artifacts {
    pub model: String,
    pub source: ModelSource,
    pub revision: String,
    pub tokenizer: String,
    #[serde(rename = "tokenizerRevision")]
    pub tokenizer_revision: String,
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
#[serde(deny_unknown_fields)]
pub struct Lifecycle {
    #[serde(rename = "startupSeconds")]
    pub startup_seconds: u64,
    #[serde(rename = "drainSeconds")]
    pub drain_seconds: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(tag = "kind", deny_unknown_fields)]
pub enum KvPlan {
    #[serde(rename = "none")]
    None { events: bool },
    #[serde(rename = "pd")]
    Pd {
        role: KvRole,
        protocol: MooncakeProtocol,
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
        role: KvRole,
        protocol: MooncakeProtocol,
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

impl KvRole {
    fn as_str(self) -> &'static str {
        match self {
            Self::KvBoth => "kv_both",
            Self::KvProducer => "kv_producer",
            Self::KvConsumer => "kv_consumer",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
pub enum MooncakeProtocol {
    #[serde(rename = "rdma")]
    Rdma,
}
impl MooncakeProtocol {
    fn as_str(self) -> &'static str {
        "rdma"
    }
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

impl EcRole {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Producer => "ec_producer",
            Self::Consumer => "ec_consumer",
        }
    }
}

impl EcTransferPlan {
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
            "ec_connector": self.connector,
            "ec_role": role.as_str(),
            "ec_connector_extra_config": {
                "shared_storage_path": self.shared_storage_path,
            },
        }))
    }

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
}

impl LaunchPlanV1 {
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
        if self.node_count > 1 && self.parallelism.pcp != 1 {
            return Err(
                "multi-node vLLM multiprocessing requires prefill context parallelism 1".into(),
            );
        }
        for (name, value) in [
            ("model", &self.artifacts.model),
            ("revision", &self.artifacts.revision),
            ("tokenizer", &self.artifacts.tokenizer),
            ("tokenizerRevision", &self.artifacts.tokenizer_revision),
        ] {
            if value.is_empty() {
                return Err(format!("launch plan artifacts.{name} must be nonempty"));
            }
        }
        let p = &self.parallelism;
        if p.tp == 0 || p.pp == 0 || p.dp == 0 || p.pcp == 0 || p.dcp == 0 {
            return Err("launch plan topology values must be positive".into());
        }
        if !(p.tp * p.pp * p.pcp * p.dp).is_multiple_of(self.node_count) {
            return Err("worker count must divide evenly across model nodes".into());
        }
        if p.pcp > 1 && p.dp > 1 {
            return Err(
                "prefill context parallelism greater than 1 requires data parallelism 1".into(),
            );
        }
        if p.pcp == 1 && !p.tp.is_multiple_of(p.dcp) {
            return Err("decode context parallelism must divide tensor parallelism".into());
        }
        if p.pcp > 1 && p.dcp != 1 && p.dcp != p.pcp && p.dcp != p.tp * p.pcp {
            return Err("decode context parallelism is incompatible with tensor and prefill context parallelism".into());
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

    /// Resolves the image's Python interpreter for engine launch and native report inspection.
    pub fn python_executable(&self) -> String {
        std::env::var(VLLM_PYTHON_ENV)
            .ok()
            .filter(|python| !python.is_empty())
            .unwrap_or_else(|| DEFAULT_VLLM_PYTHON.into())
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

    /// Returns provider environment for the managed vLLM child process.
    pub fn source_environment(&self, model_root: &Path) -> Vec<(String, String)> {
        let use_modelscope = self.artifacts.source == ModelSource::ModelScope;
        let mut environment = vec![(VLLM_USE_MODELSCOPE_ENV.into(), use_modelscope.to_string())];
        if use_modelscope {
            environment.extend([
                (
                    foretoken_artifacts::MODELSCOPE_CACHE_ENV.into(),
                    foretoken_artifacts::modelscope_cache_root(model_root)
                        .display()
                        .to_string(),
                ),
                (
                    foretoken_artifacts::MODELSCOPE_DOMAIN_ENV.into(),
                    foretoken_artifacts::DEFAULT_MODELSCOPE_DOMAIN.into(),
                ),
            ]);
        }
        environment
    }

    /// Builds the owned managed-engine configuration consumed by model-server startup.
    ///
    /// The model-server image selects Python through `FORETOKEN_VLLM_PYTHON`; the process handle
    /// takes the resulting configuration, while the plan contributes validated vLLM flags.
    pub fn managed_engine(
        &self,
        handshake_port: u16,
        member: Option<&crate::config::MemberContext>,
    ) -> Result<ManagedEngineConfig, String> {
        let mut config = ManagedEngineConfig {
            python: self.python_executable(),
            model: self.artifacts.model.clone(),
            handshake_host: LOOPBACK_HOST.into(),
            handshake_port,
            data_parallel_size: self.parallelism.dp,
            python_args: self.render_vllm_args(member)?,
        };
        if let Some(member) = member {
            config.handshake_host = member.leader_address.clone();
            config.python_args.extend([
                format!("--nnodes={}", self.node_count),
                format!("--node-rank={}", member.index),
                format!("--master-addr={}", member.leader_address),
                "--master-port=29800".into(),
                "--distributed-executor-backend=mp".into(),
                "--data-parallel-backend=mp".into(),
            ]);
        }
        Ok(config)
    }

    /// Renders the owned vLLM arguments consumed by the managed-engine child process.
    ///
    /// Validation runs before rendering; the returned vector does not borrow the launch plan.
    pub fn render_vllm_args(
        &self,
        member: Option<&crate::config::MemberContext>,
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
        // The controller has already normalized native option names.
        // Keep argument values intact: this command never goes through a shell.
        for (name, value) in &self.engine_args {
            match value {
                serde_json::Value::Null => {}
                serde_json::Value::Bool(enabled) => args.push(if *enabled {
                    format!("--{name}")
                } else {
                    format!("--no-{name}")
                }),
                serde_json::Value::String(value) => args.push(format!("--{name}={value}")),
                serde_json::Value::Array(values) => {
                    args.push(format!("--{name}"));
                    for value in values {
                        let value = match value {
                            serde_json::Value::String(value) => value.clone(),
                            value => value.to_string(),
                        };
                        // A list item must not become a separate CLI option.
                        if value.starts_with('-') && value.parse::<f64>().is_err() {
                            return Err(format!(
                                "engineArgs.{name} contains an option-like list value"
                            ));
                        }
                        args.push(value);
                    }
                }
                value => args.push(format!("--{name}={value}")),
            }
        }
        if matches!(self.ec.role, Some(EcRole::Producer)) {
            args.push("--no-enable-prefix-caching".into());
        }
        if self.kv.events() {
            args.push(format!("--kv-events-config={}", json!({"publisher":"zmq","endpoint":kv_event_endpoint(member.map_or(LOOPBACK_HOST, |member| member.leader_address.as_str()), 0),"topic":KV_EVENT_TOPIC,"enable_kv_cache_events":true,"hwm":4096,"max_queue_size":4096})));
        }
        if let Some(config) = self.kv.transfer_config(self.shared_prefix_lookup()) {
            args.push(format!("--kv-transfer-config={config}"));
        }
        if let Some(config) = self.ec.transfer_config() {
            args.push(format!("--ec-transfer-config={config}"));
        }
        Ok(args)
    }
}

impl LaunchPlanV1 {
    /// Returns the connector-owned placement exposed by live prefix observation.
    pub fn shared_prefix_placement(&self) -> Option<KvPlacement> {
        match self.kv {
            KvPlan::FilesystemOffload { .. } => Some(KvPlacement {
                tier: KvStorageTier::Disk,
                locality: KvCacheLocality::Local,
            }),
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

impl KvPlan {
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
    // Map each validated KV plan to the vLLM child-process contract. The rendered value is owned
    // by argv construction, keeping controller plan fields separate from backend-specific JSON.
    fn transfer_config(&self, shared_prefix_lookup: bool) -> Option<serde_json::Value> {
        let pd = |role: KvRole, protocol: MooncakeProtocol, device_name: &str| json!({"kv_connector":"MooncakeConnector","kv_role":role.as_str(),"kv_connector_extra_config":{"mooncake_protocol":protocol.as_str(),"device_name":device_name}});
        let store = |role: KvRole| {
            let mut config = json!({"kv_connector":"MooncakeStoreConnector","kv_role":role.as_str(),"kv_load_failure_policy":"recompute"});
            if shared_prefix_lookup {
                config["kv_connector_module_path"] =
                    json!(crate::shared_kv::MOONCAKE_CONNECTOR_MODULE);
            }
            config
        };
        match self {
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
                json!({"kv_connector":"OffloadingConnector","kv_connector_module_path":crate::shared_kv::OFFLOADING_CONNECTOR_MODULE,"kv_role":"kv_both","kv_connector_extra_config":{"cpu_bytes_to_use":cpu_bytes,"spec_name":"TieringOffloadingSpec","secondary_tiers":[{"type":"fs","root_dir":storage_path,"enable_kv_events":events}]}}),
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
        }
    }
}

fn is_absolute_storage_path(value: &str) -> bool {
    value.starts_with('/') && value != "/" && !value.contains(char::is_whitespace)
}
