// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Private versioned launch contract and the sole vLLM argv renderer.

use std::collections::HashSet;
use std::time::Duration;

use serde::Deserialize;
use serde_json::json;
use vllm_managed_engine::ManagedEngineConfig;

use foretoken_model_protocol::RuntimeEcTransferMetadata;

const HANDSHAKE_HOST: &str = "127.0.0.1";
const KV_OFFLOAD_ROOT: &str = "/var/lib/foretoken/kv-offload";
const PYTHON: &str = "python3";
const MIN_INTERNAL_GENERATE_REQUEST_BODY_LIMIT_BYTES: usize = 1024 * 1024;
const MAX_INTERNAL_GENERATE_REQUEST_BODY_LIMIT_BYTES: usize = 256 * 1024 * 1024;

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LaunchPlanV1 {
    pub version: u8,
    pub artifacts: Artifacts,
    pub parallelism: Parallelism,
    pub kv: KvPlan,
    #[serde(default)]
    pub ec: EcTransferPlan,
    pub lifecycle: Lifecycle,
    #[serde(rename = "internalGenerateRequestBodyLimitBytes")]
    pub internal_generate_request_body_limit_bytes: usize,
    #[serde(rename = "extraArgs")]
    pub extra_args: Vec<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Artifacts {
    pub model: String,
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
        #[serde(rename = "deviceName")]
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
        events: bool,
    },
    #[serde(rename = "mooncakeStore")]
    MooncakeStore { role: KvRole, events: bool },
    #[serde(rename = "multiConnector")]
    MultiConnector {
        role: KvRole,
        protocol: MooncakeProtocol,
        #[serde(rename = "deviceName")]
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
/// The the local vLLM build source release exposes `ECExampleConnector` as its reference
/// disaggregated-encoder path. Encoder and prefill Pods share one platform-owned
/// ReadWriteMany volume; no client-controlled connector options are accepted.
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
            || !self.shared_storage_path.starts_with('/')
            || self.shared_storage_path.contains(char::is_whitespace)
        {
            return Err("EC transfer config is incomplete or unsupported".into());
        }
        Ok(())
    }

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

    pub fn enabled(&self) -> bool {
        !self.connector.is_empty()
    }

    pub fn runtime_metadata(&self) -> Option<RuntimeEcTransferMetadata> {
        Some(RuntimeEcTransferMetadata {
            role: self.role?.as_str().into(),
            profile: self.profile_name.clone(),
            connector: self.connector.clone(),
        })
    }
}

impl LaunchPlanV1 {
    pub fn parse(input: &str) -> Result<Self, String> {
        let plan: Self = serde_json::from_str(input)
            .map_err(|error| format!("invalid FORETOKEN_VLLM_LAUNCH_PLAN: {error}"))?;
        plan.validate()?;
        Ok(plan)
    }

    pub fn validate(&self) -> Result<(), String> {
        if self.version != 1 {
            return Err("launch plan version must be 1".into());
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
        if !(MIN_INTERNAL_GENERATE_REQUEST_BODY_LIMIT_BYTES
            ..=MAX_INTERNAL_GENERATE_REQUEST_BODY_LIMIT_BYTES)
            .contains(&self.internal_generate_request_body_limit_bytes)
        {
            return Err("launch plan internalGenerateRequestBodyLimitBytes must be between 1048576 and 268435456".into());
        }
        if self.kv.events() != (p.dp == 1) {
            return Err("KV events must be enabled exactly when DP is 1".into());
        }
        match &self.kv {
            KvPlan::CpuOffload { cpu_bytes, .. } | KvPlan::FilesystemOffload { cpu_bytes, .. }
                if *cpu_bytes <= 0 =>
            {
                return Err("KV offload cpuBytes must be positive".into());
            }
            KvPlan::Pd { device_name, .. } | KvPlan::MultiConnector { device_name, .. }
                if device_name.trim().is_empty() =>
            {
                return Err("P/D KV plans require a platform-owned RDMA device name".into());
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
        self.ec.validate()?;
        validate_extra_args(&self.extra_args)
    }

    pub fn startup_timeout(&self) -> Duration {
        Duration::from_secs(self.lifecycle.startup_seconds)
    }
    pub fn drain_timeout(&self) -> Duration {
        Duration::from_secs(self.lifecycle.drain_seconds)
    }

    // ManagedEngineConfig owns positional model, headless mode, loopback handshake,
    // and data-parallel wiring; this plan contributes only its rendered vLLM flags.
    pub fn managed_engine(&self, handshake_port: u16) -> Result<ManagedEngineConfig, String> {
        Ok(ManagedEngineConfig {
            python: PYTHON.into(),
            model: self.artifacts.model.clone(),
            handshake_host: HANDSHAKE_HOST.into(),
            handshake_port,
            data_parallel_size: self.parallelism.dp,
            python_args: self.render_vllm_args()?,
        })
    }

    pub fn render_vllm_args(&self) -> Result<Vec<String>, String> {
        self.validate()?;
        let p = &self.parallelism;
        let mut args = vec![
            format!("--revision={}", self.artifacts.revision),
            format!("--tokenizer={}", self.artifacts.tokenizer),
            format!("--tokenizer-revision={}", self.artifacts.tokenizer_revision),
            format!("--tensor-parallel-size={}", p.tp),
            format!("--pipeline-parallel-size={}", p.pp),
            format!("--prefill-context-parallel-size={}", p.pcp),
            format!("--decode-context-parallel-size={}", p.dcp),
        ];
        if let Some(ep) = &p.ep {
            args.push("--enable-expert-parallel".into());
            if !ep.backend.is_empty() {
                args.push(format!("--all2all-backend={}", ep.backend));
            }
            if ep.eplb {
                args.push("--enable-eplb".into());
            }
        }
        args.extend(self.extra_args.clone());
        if matches!(self.ec.role, Some(EcRole::Producer)) {
            args.push("--no-enable-prefix-caching".into());
        }
        if self.kv.events() {
            args.push(format!("--kv-events-config={}", json!({"publisher":"zmq","endpoint":"tcp://127.0.0.1:5557","replay_endpoint":"tcp://127.0.0.1:5558","topic":"foretoken-kv-v1","enable_kv_cache_events":true,"buffer_steps":1024,"hwm":4096,"max_queue_size":4096})));
        }
        if let Some(config) = self.kv.transfer_config() {
            args.push(format!("--kv-transfer-config={config}"));
        }
        if let Some(config) = self.ec.transfer_config() {
            args.push(format!("--ec-transfer-config={config}"));
        }
        args.push(format!(
            "--shutdown-timeout={}",
            self.lifecycle.drain_seconds
        ));
        Ok(args)
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
    fn transfer_config(&self) -> Option<serde_json::Value> {
        let pd = |role: KvRole, protocol: MooncakeProtocol, device_name: &str| json!({"kv_connector":"MooncakeConnector","kv_role":role.as_str(),"kv_connector_extra_config":{"mooncake_protocol":protocol.as_str(),"device_name":device_name}});
        match self {
            Self::None { .. } => None,
            Self::Pd {
                role,
                protocol,
                device_name,
                ..
            } => Some(pd(*role, *protocol, device_name)),
            Self::CpuOffload { cpu_bytes, .. } => Some(
                json!({"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_connector_extra_config":{"cpu_bytes_to_use":cpu_bytes,"kv_connector":"CPUOffloadingSpec"}}),
            ),
            Self::FilesystemOffload { cpu_bytes, .. } => Some(
                json!({"kv_connector":"OffloadingConnector","kv_role":"kv_both","kv_connector_extra_config":{"cpu_bytes_to_use":cpu_bytes,"kv_connector":"TieringOffloadingSpec","secondary_tiers":[{"type":"fs","root_dir":KV_OFFLOAD_ROOT}]}}),
            ),
            Self::MooncakeStore { role, .. } => Some(
                json!({"kv_connector":"MooncakeStoreConnector","kv_role":role.as_str(),"kv_load_failure_policy":"recompute"}),
            ),
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
                    json!({"kv_connector":"MultiConnector","kv_role":role.as_str(),"kv_load_failure_policy":"recompute","kv_connector_extra_config":{"connectors":[pd(*role, *protocol, device_name), {"kv_connector":"MooncakeStoreConnector","kv_role":store_role.as_str()}]}}),
                )
            }
        }
    }
}

fn validate_extra_args(args: &[String]) -> Result<(), String> {
    const VALUE_FLAGS: &[&str] = &[
        "--max-model-len",
        "--dtype",
        "--quantization",
        "--gpu-memory-utilization",
        "--max-num-seqs",
        "--max-num-batched-tokens",
    ];
    const BOOL_FLAGS: &[&str] = &["--enforce-eager", "--disable-log-stats"];
    let mut seen = HashSet::new();
    for argument in args {
        if argument.is_empty()
            || argument.contains(char::is_whitespace)
            || !argument.starts_with("--")
            || argument == "--"
        {
            return Err("extraArgs must be one nonempty --long-name token".into());
        }
        let (name, value) = argument
            .split_once('=')
            .map_or((argument.as_str(), None), |(name, value)| {
                (name, Some(value))
            });
        if argument.matches('=').count() > 1 || name.contains('_') || !seen.insert(name) {
            return Err(format!("extraArgs flag {name:?} is not allowed"));
        }
        if VALUE_FLAGS.contains(&name) {
            if value.is_none_or(str::is_empty) {
                return Err(format!("extraArgs flag {name:?} requires a value"));
            }
        } else if BOOL_FLAGS.contains(&name) {
            if value.is_some() {
                return Err(format!("extraArgs flag {name:?} does not take a value"));
            }
        } else {
            return Err(format!("extraArgs flag {name:?} is not allowed"));
        }
    }
    Ok(())
}
