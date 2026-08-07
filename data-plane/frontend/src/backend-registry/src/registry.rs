// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//! Runtime backend registry, health refresh, telemetry, and facade resolution.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use foretoken_llm_facade::{HttpFacade, LlmFacade, LlmFacadeResolver, VllmFacade};
use foretoken_model_protocol::{
    RuntimeEcTransferMetadata, RuntimeMetadataResponse, TelemetryResponse, VLLM_PINNED_REVISION,
};
use vllm_engine_core_client::protocol::dtype::ModelDtype;

use foretoken_router::{
    AdmissionTarget, BackendId, BackendRole, BackendRoute, ComponentCapacity, ExecutionPlan,
    ModelRouter, RouteInventory,
};

use crate::snapshot::{ServingSnapshot, SnapshotError};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteTable {
    version: u64,
    router: ModelRouter,
}
impl RouteTable {
    pub(crate) fn new(version: u64, routes: Vec<BackendRoute>) -> Self {
        Self {
            version,
            router: ModelRouter::new(routes),
        }
    }

    pub fn version(&self) -> u64 {
        self.version
    }
    pub fn router(&self) -> &ModelRouter {
        &self.router
    }
    pub fn routes(&self) -> &[BackendRoute] {
        self.router.routes()
    }
}
pub struct BackendRegistry {
    table: RouteTable,
    components: BTreeMap<BackendId, Component>,
    health: BTreeMap<BackendId, AtomicBool>,
    capacity: Mutex<BTreeMap<BackendId, ComponentCapacity>>,
    metadata: Mutex<BTreeMap<BackendId, RuntimeMetadataResponse>>,
    health_client: reqwest::Client,
}
#[derive(Debug, Clone)]
pub(crate) struct RuntimeExpectation {
    pub model: String,
    pub revision: String,
    pub ec_transfer: Option<RuntimeEcTransferMetadata>,
}
pub(crate) enum Component {
    Aggregate {
        endpoint: String,
        facade: Arc<HttpFacade>,
        expected: RuntimeExpectation,
    },
    Encoder {
        endpoint: String,
        expected: RuntimeExpectation,
    },
    Prefill {
        endpoint: String,
        bootstrap: String,
        expected: RuntimeExpectation,
    },
    Decode {
        endpoint: String,
        expected: RuntimeExpectation,
    },
}

impl Component {
    fn endpoint(&self) -> &str {
        match self {
            Self::Aggregate { endpoint, .. }
            | Self::Encoder { endpoint, .. }
            | Self::Prefill { endpoint, .. }
            | Self::Decode { endpoint, .. } => endpoint,
        }
    }

    fn expected(&self) -> &RuntimeExpectation {
        match self {
            Self::Aggregate { expected, .. }
            | Self::Encoder { expected, .. }
            | Self::Prefill { expected, .. }
            | Self::Decode { expected, .. } => expected,
        }
    }

    fn bootstrap(&self) -> Option<&str> {
        match self {
            Self::Prefill { bootstrap, .. } => Some(bootstrap),
            Self::Aggregate { .. } | Self::Encoder { .. } | Self::Decode { .. } => None,
        }
    }
}
impl BackendRegistry {
    pub fn from_json(bytes: &[u8]) -> Result<Self, SnapshotError> {
        Self::from_snapshot(serde_json::from_slice(bytes).map_err(SnapshotError::Parse)?)
    }
    pub fn from_snapshot(snapshot: ServingSnapshot) -> Result<Self, SnapshotError> {
        let (table, components) = crate::build::build(snapshot)?;
        let health = components
            .keys()
            .map(|id| (id.clone(), AtomicBool::new(false)))
            .collect();
        Ok(Self {
            table,
            components,
            health,
            capacity: Mutex::new(BTreeMap::new()),
            metadata: Mutex::new(BTreeMap::new()),
            health_client: reqwest::Client::builder()
                .timeout(Duration::from_secs(1))
                .build()
                .expect("static client"),
        })
    }
    pub fn route_table(&self) -> &RouteTable {
        &self.table
    }

    /// Returns the last metadata response that passed this component's snapshot checks.
    pub fn metadata(&self, id: &BackendId) -> Option<RuntimeMetadataResponse> {
        self.metadata.lock().ok()?.get(id).cloned()
    }

    pub fn is_ready(&self) -> bool {
        !self.healthy_models().is_empty()
    }

    /// Returns the safe effective context limit across healthy components for a model.
    pub fn effective_max_model_len(&self, model: &str) -> Option<u32> {
        self.table
            .routes()
            .iter()
            .filter(|route| route.model == model && self.is_backend_healthy(&route.backend_id))
            .filter_map(|route| self.metadata(&route.backend_id))
            .map(|metadata| metadata.effective_max_model_len)
            .min()
    }

    /// Returns one consistent engine-reported dtype across healthy components.
    pub fn effective_model_dtype(&self, model: &str) -> Option<ModelDtype> {
        let mut dtypes = self
            .table
            .routes()
            .iter()
            .filter(|route| route.model == model && self.is_backend_healthy(&route.backend_id))
            .filter_map(|route| self.metadata(&route.backend_id))
            .map(|metadata| metadata.model_dtype);
        let dtype = dtypes.next()?;
        dtypes.all(|candidate| candidate == dtype).then_some(dtype)
    }

    pub fn healthy_models(&self) -> Vec<String> {
        let mut models = BTreeSet::new();
        let mut pd_domains = BTreeMap::<(String, String), (bool, bool, bool)>::new();
        for route in self
            .table
            .routes()
            .iter()
            .filter(|route| self.is_backend_healthy(&route.backend_id))
        {
            match route.role {
                BackendRole::Aggregate => {
                    models.insert(route.model.clone());
                }
                BackendRole::Encoder | BackendRole::Prefill | BackendRole::Decode => {
                    let Some(domain_id) = route.domain_id.as_ref() else {
                        continue;
                    };
                    let roles = pd_domains
                        .entry((route.model.clone(), domain_id.clone()))
                        .or_default();
                    match route.role {
                        BackendRole::Encoder => roles.0 = true,
                        BackendRole::Prefill => roles.1 = true,
                        BackendRole::Decode => roles.2 = true,
                        BackendRole::Aggregate => unreachable!("aggregate was handled above"),
                    }
                }
            }
        }
        models.extend(pd_domains.into_iter().filter_map(
            |((model, domain_id), (encoder, prefill, decode))| {
                let requires_encoder = self.table.routes().iter().any(|route| {
                    route.model == model
                        && route.domain_id.as_deref() == Some(domain_id.as_str())
                        && route.role == BackendRole::Encoder
                });
                ((!requires_encoder || encoder) && prefill && decode).then_some(model)
            },
        ));
        models.into_iter().collect()
    }
    /// Health, runtime metadata, and telemetry are per physical component.
    pub async fn refresh_backend_readiness(&self) {
        let probes = self.components.iter().map(|(id, c)| async move {
            let ready = ready(&self.health_client, c.endpoint()).await;
            let metadata = metadata(&self.health_client, c.endpoint()).await;
            let bootstrap = match c.bootstrap() {
                Some(endpoint) => {
                    foretoken_llm_facade::bootstrap_engine_id(&self.health_client, endpoint)
                        .await
                        .is_ok()
                }
                None => true,
            };
            let telemetry = telemetry(&self.health_client, id, c.endpoint()).await;
            let metadata_valid = metadata
                .as_ref()
                .is_some_and(|value| metadata_matches(c.expected(), value));
            (
                id.clone(),
                ready && bootstrap && metadata_valid,
                metadata,
                telemetry,
            )
        });
        let mut results = futures::future::join_all(probes).await;
        invalidate_incompatible_pd_pairs(&self.table, &mut results);

        let mut next_capacity = BTreeMap::new();
        let mut next_metadata = BTreeMap::new();
        for (id, healthy, metadata, capacity) in results {
            let healthy = healthy && capacity.is_some();
            self.health
                .get(&id)
                .expect("component health")
                .store(healthy, Ordering::Release);
            if healthy {
                next_capacity.insert(id.clone(), capacity.expect("checked above"));
                next_metadata.insert(id, metadata.expect("validated above"));
            }
        }
        *self.capacity.lock().expect("capacity mutex") = next_capacity;
        *self.metadata.lock().expect("metadata mutex") = next_metadata;
    }
}
impl LlmFacadeResolver for BackendRegistry {
    fn resolve(&self, plan: &ExecutionPlan) -> Option<Arc<dyn LlmFacade>> {
        match plan {
            ExecutionPlan::Aggregate { decision } => {
                match self.components.get(&decision.backend_id)? {
                    Component::Aggregate { facade, .. } => Some(facade.clone()),
                    Component::Encoder { .. }
                    | Component::Prefill { .. }
                    | Component::Decode { .. } => None,
                }
            }
            ExecutionPlan::PrefillDecode { prefill, decode } => {
                let p = self.components.get(&prefill.backend_id)?;
                let d = self.components.get(&decode.backend_id)?;
                Some(Arc::new(
                    VllmFacade::prefill_decode(
                        p.endpoint().to_owned(),
                        d.endpoint().to_owned(),
                        p.bootstrap()?.to_owned(),
                    )
                    .ok()?,
                ))
            }
            ExecutionPlan::EncoderPrefillDecode {
                encoder,
                prefill,
                decode,
            } => {
                let e = self.components.get(&encoder.backend_id)?;
                let p = self.components.get(&prefill.backend_id)?;
                let d = self.components.get(&decode.backend_id)?;
                Some(Arc::new(
                    VllmFacade::encoder_prefill_decode(
                        e.endpoint().to_owned(),
                        p.endpoint().to_owned(),
                        d.endpoint().to_owned(),
                        p.bootstrap()?.to_owned(),
                    )
                    .ok()?,
                ))
            }
        }
    }
}
impl RouteInventory for BackendRegistry {
    fn model_router(&self) -> &ModelRouter {
        self.table.router()
    }
    fn is_backend_healthy(&self, id: &BackendId) -> bool {
        self.health
            .get(id)
            .is_some_and(|v| v.load(Ordering::Acquire))
    }
    fn admission_target(&self, id: &BackendId) -> Option<AdmissionTarget> {
        self.capacity
            .lock()
            .ok()?
            .get(id)
            .cloned()
            .map(|c| AdmissionTarget {
                components: vec![c],
            })
    }
    fn effective_capabilities(&self, id: &BackendId) -> BTreeSet<String> {
        let Some(policy) = self
            .table
            .routes()
            .iter()
            .find(|route| &route.backend_id == id)
            .map(|route| &route.capabilities)
        else {
            return BTreeSet::new();
        };
        let Ok(metadata) = self.metadata.lock() else {
            return BTreeSet::new();
        };
        let Some(observed) = metadata.get(id) else {
            return BTreeSet::new();
        };
        // Chat/text/tool/reasoning/structured-output and multimodal preprocessing are
        // frontend-owned. Only capabilities proved by EngineCore metadata are gated here.
        policy
            .iter()
            .filter(|capability| {
                !requires_runtime_observation(capability)
                    || observed.capabilities.contains(capability.as_str())
            })
            .cloned()
            .collect()
    }
}
fn requires_runtime_observation(capability: &str) -> bool {
    matches!(capability, "lora")
}

fn metadata_matches(expected: &RuntimeExpectation, metadata: &RuntimeMetadataResponse) -> bool {
    metadata.version == 1
        && metadata.model.model == expected.model
        && metadata.model.revision == expected.revision
        && metadata.ec_transfer == expected.ec_transfer
        && metadata.vllm_pinned_revision == VLLM_PINNED_REVISION
        && metadata.effective_max_model_len > 0
}

fn invalidate_incompatible_pd_pairs(
    table: &RouteTable,
    results: &mut [(
        BackendId,
        bool,
        Option<RuntimeMetadataResponse>,
        Option<ComponentCapacity>,
    )],
) {
    let mut domains = BTreeMap::<String, Vec<usize>>::new();
    for route in table.routes() {
        if matches!(
            route.role,
            BackendRole::Encoder | BackendRole::Prefill | BackendRole::Decode
        ) {
            let Some(domain) = route.domain_id.as_ref() else {
                continue;
            };
            if let Some(index) = results.iter().position(|(id, ..)| id == &route.backend_id) {
                domains.entry(domain.clone()).or_default().push(index);
            }
        }
    }
    for indices in domains.into_values() {
        let metadata: Vec<&RuntimeMetadataResponse> = indices
            .iter()
            .filter_map(|index| {
                let (_, healthy, metadata, _) = &results[*index];
                (*healthy).then_some(metadata.as_ref()?)
            })
            .collect();
        if metadata.len() == indices.len()
            && metadata.windows(2).any(|pair| {
                pair[0].model_dtype != pair[1].model_dtype
                    || pair[0].effective_max_model_len != pair[1].effective_max_model_len
            })
        {
            for index in indices {
                results[index].1 = false;
            }
        }
    }
}

async fn metadata(client: &reqwest::Client, endpoint: &str) -> Option<RuntimeMetadataResponse> {
    let response = client
        .get(format!(
            "{}/v1/internal/metadata",
            endpoint.trim_end_matches('/')
        ))
        .send()
        .await
        .ok()?;
    response
        .status()
        .is_success()
        .then_some(response.json().await.ok()?)
}

async fn telemetry(
    client: &reqwest::Client,
    id: &BackendId,
    endpoint: &str,
) -> Option<ComponentCapacity> {
    let response = client
        .get(format!(
            "{}/v1/internal/telemetry",
            endpoint.trim_end_matches('/')
        ))
        .send()
        .await
        .ok()?;
    if !response.status().is_success() {
        return None;
    }
    let response: TelemetryResponse = response.json().await.ok()?;
    (response.version == 1 && response.accepting).then_some(ComponentCapacity {
        component_id: id.clone(),
        running_requests: response.running_requests,
        max_concurrent_requests: response.max_concurrent_requests,
    })
}
async fn ready(client: &reqwest::Client, endpoint: &str) -> bool {
    matches!(client.get(format!("{}/readyz",endpoint.trim_end_matches('/'))).send().await,Ok(response) if response.status().is_success())
}

#[cfg(test)]
#[path = "tests/registry.rs"]
mod tests;
