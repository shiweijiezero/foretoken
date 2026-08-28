// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Runtime backend registry, health refresh, telemetry, and facade resolution.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use foretoken_engine_core_client::protocol::dtype::ModelDtype;
use foretoken_llm_facade::{HttpFacade, LlmFacade, LlmFacadeResolver, RouteStage};
use foretoken_model_protocol::{RuntimeMetadataResponse, TelemetryResponse};

use foretoken_model_protocol::ModelServerRole;
use foretoken_router::{
    ModelRouteTable, RouteDecision, RouteInventory, RouteTargetId, RouteTargetStats,
    RouteTargetStatsReader,
};

use crate::route_target_stats::RouteTargetStatsHistory;
use crate::snapshot::{ServingSnapshot, SnapshotError};

const ROUTE_TARGET_STATS_RETENTION: Duration = Duration::from_secs(300);

pub struct BackendRegistry {
    model_routes: ModelRouteTable,
    configured_models: BTreeSet<String>,
    components: BTreeMap<RouteTargetId, Component>,
    health: BTreeMap<RouteTargetId, AtomicBool>,
    stats: Mutex<BTreeMap<RouteTargetId, RouteTargetStatsHistory>>,
    metadata: Mutex<BTreeMap<RouteTargetId, RuntimeMetadataResponse>>,
    health_client: reqwest::Client,
}
pub(crate) enum Component {
    Aggregate {
        endpoint: String,
        facade: Arc<HttpFacade>,
    },
    Encoder {
        endpoint: String,
    },
    Prefill {
        endpoint: String,
        bootstrap: String,
    },
    Decode {
        endpoint: String,
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

    fn bootstrap(&self) -> Option<&str> {
        match self {
            Self::Prefill { bootstrap, .. } => Some(bootstrap),
            Self::Aggregate { .. } | Self::Encoder { .. } | Self::Decode { .. } => None,
        }
    }
}
impl BackendRegistry {
    /// Builds the frontend-owned route inventory and component clients from a serving snapshot.
    ///
    /// Startup retains the registry for routing, readiness refresh, and facade resolution; the input snapshot is consumed.
    pub fn from_snapshot(snapshot: ServingSnapshot) -> Result<Self, SnapshotError> {
        let configured_models = snapshot.admission_target_sets()?.into_keys().collect();
        let (model_routes, components) = crate::snapshot_projection::project_registry(snapshot)?;
        let health = components
            .keys()
            .map(|id| (id.clone(), AtomicBool::new(false)))
            .collect();
        Ok(Self {
            model_routes,
            configured_models,
            components,
            health,
            stats: Mutex::new(BTreeMap::new()),
            metadata: Mutex::new(BTreeMap::new()),
            health_client: reqwest::Client::builder()
                .timeout(Duration::from_secs(1))
                .build()
                .expect("static client"),
        })
    }
    /// Returns an owned list of logical model names declared by the current snapshot.
    pub fn configured_models(&self) -> Vec<String> {
        self.configured_models.iter().cloned().collect()
    }

    /// Reports whether the current snapshot declares at least one logical model.
    pub fn is_configured(&self) -> bool {
        !self.configured_models.is_empty()
    }

    fn metadata(&self, id: &RouteTargetId) -> Option<RuntimeMetadataResponse> {
        self.metadata.lock().ok()?.get(id).cloned()
    }

    /// Reports whether any configured model currently has an executable backend path.
    pub fn is_ready(&self) -> bool {
        !self.healthy_models().is_empty()
    }

    fn metadata_matches_route(
        &self,
        id: &RouteTargetId,
        metadata: &RuntimeMetadataResponse,
    ) -> bool {
        self.model_routes.routes().iter().any(|route| {
            route.route_target_id == *id
                && metadata.model.model == route.model
                && metadata.model.revision == route.revision
        })
    }

    /// Returns the safe effective context limit across healthy components for a model.
    pub fn effective_max_model_len(&self, model: &str) -> Option<u32> {
        self.model_routes
            .routes()
            .iter()
            .filter(|route| {
                route.model == model && self.is_route_target_healthy(&route.route_target_id)
            })
            .filter_map(|route| self.metadata(&route.route_target_id))
            .map(|metadata| metadata.effective_max_model_len)
            .min()
    }

    /// Returns one consistent engine-reported dtype across healthy components.
    pub fn effective_model_dtype(&self, model: &str) -> Option<ModelDtype> {
        let mut dtypes = self
            .model_routes
            .routes()
            .iter()
            .filter(|route| {
                route.model == model && self.is_route_target_healthy(&route.route_target_id)
            })
            .filter_map(|route| self.metadata(&route.route_target_id))
            .map(|metadata| metadata.model_dtype);
        let dtype = dtypes.next()?;
        dtypes.all(|candidate| candidate == dtype).then_some(dtype)
    }

    /// Reports whether one logical model currently has an executable backend path.
    pub fn is_model_ready(&self, model: &str) -> bool {
        self.healthy_models().iter().any(|healthy| healthy == model)
    }

    /// Lists models with a currently executable aggregate route or complete split pipeline.
    pub fn healthy_models(&self) -> Vec<String> {
        // Aggregate routes are independently serviceable. A split scope is healthy only with
        // P+D, or E+P+D when that scope includes an encoder.
        let mut models = BTreeSet::new();
        let mut pipeline_scopes = BTreeMap::<(String, String), (bool, bool, bool)>::new();
        for route in self.model_routes.routes() {
            if route.role == ModelServerRole::Aggregate
                && self.is_route_target_healthy(&route.route_target_id)
            {
                models.insert(route.model.clone());
                continue;
            }
            let Some(pipeline_scope_id) = &route.pipeline_scope_id else {
                continue;
            };
            let roles = pipeline_scopes
                .entry((route.model.clone(), pipeline_scope_id.clone()))
                .or_default();
            if self.is_route_target_healthy(&route.route_target_id) {
                match route.role {
                    ModelServerRole::Encoder => roles.0 = true,
                    ModelServerRole::Prefill => roles.1 = true,
                    ModelServerRole::Decode => roles.2 = true,
                    ModelServerRole::Aggregate => {}
                }
            }
        }
        for ((model, pipeline_scope_id), (encoder, prefill, decode)) in pipeline_scopes {
            let epd = self.model_routes.routes().iter().any(|route| {
                route.model == model
                    && route.pipeline_scope_id.as_deref() == Some(pipeline_scope_id.as_str())
                    && route.role == ModelServerRole::Encoder
            });
            if prefill && decode && (!epd || encoder) {
                models.insert(model);
            }
        }
        models.into_iter().collect()
    }
    /// Health, runtime metadata, and telemetry are per physical component.
    pub async fn refresh_backend_readiness(&self) {
        // Probe components concurrently, but retain metadata and telemetry only for components
        // proven healthy in this pass so replaced backends cannot leave stale routing signals.
        let probes = self.components.iter().map(|(id, c)| async move {
            let ready = ready(&self.health_client, c.endpoint()).await;
            let metadata = metadata(&self.health_client, c.endpoint()).await;
            let metadata_matches = metadata
                .as_ref()
                .is_some_and(|metadata| self.metadata_matches_route(id, metadata));
            let bootstrap = match c.bootstrap() {
                Some(endpoint) => {
                    foretoken_llm_facade::bootstrap_engine_id(&self.health_client, endpoint)
                        .await
                        .is_ok()
                }
                None => true,
            };
            let stats = telemetry(&self.health_client, c.endpoint()).await;
            let accepting = stats.as_ref().is_some_and(|stats| stats.accepting);
            (
                id.clone(),
                ready && bootstrap && metadata_matches && accepting,
                metadata,
                stats,
            )
        });
        let results = futures::future::join_all(probes).await;
        let mut next_metadata = BTreeMap::new();
        let mut histories = self.stats.lock().expect("backend stats mutex");
        for (id, healthy, metadata, stats) in results {
            self.health
                .get(&id)
                .expect("component health")
                .store(healthy, Ordering::Release);
            if healthy {
                if let Some(stats) = stats {
                    histories
                        .entry(id.clone())
                        .or_insert_with(|| {
                            RouteTargetStatsHistory::new(ROUTE_TARGET_STATS_RETENTION)
                        })
                        .push(stats);
                } else {
                    histories.remove(&id);
                }
                next_metadata.insert(id, metadata.expect("validated above"));
            } else {
                histories.remove(&id);
            }
        }
        drop(histories);
        *self.metadata.lock().expect("metadata mutex") = next_metadata;
    }
}
impl LlmFacadeResolver for BackendRegistry {
    fn resolve_stage(
        &self,
        decision: &RouteDecision,
        stage: RouteStage,
    ) -> Option<Arc<dyn LlmFacade>> {
        let component = self.components.get(&decision.route_target_id)?;
        match (stage, component) {
            (RouteStage::Aggregate, Component::Aggregate { facade, .. }) => Some(facade.clone()),
            (RouteStage::Encoder, Component::Encoder { endpoint, .. })
            | (RouteStage::Prefill, Component::Prefill { endpoint, .. })
            | (RouteStage::Decode, Component::Decode { endpoint, .. }) => {
                Some(Arc::new(HttpFacade::new(endpoint.clone()).ok()?))
            }
            _ => None,
        }
    }

    fn bootstrap_endpoint(&self, prefill: &RouteDecision) -> Option<String> {
        self.components
            .get(&prefill.route_target_id)?
            .bootstrap()
            .map(str::to_owned)
    }
}
impl RouteInventory for BackendRegistry {
    fn model_routes(&self) -> &ModelRouteTable {
        &self.model_routes
    }
    fn is_route_target_healthy(&self, id: &RouteTargetId) -> bool {
        self.health
            .get(id)
            .is_some_and(|v| v.load(Ordering::Acquire))
    }

    fn effective_capabilities(&self, id: &RouteTargetId) -> BTreeSet<String> {
        let Some(declared) = self
            .model_routes
            .routes()
            .iter()
            .find(|route| &route.route_target_id == id)
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
        declared
            .iter()
            .filter(|capability| {
                !requires_runtime_observation(capability)
                    || observed.capabilities.contains(*capability)
            })
            .cloned()
            .collect()
    }
}

impl RouteTargetStatsReader for BackendRegistry {
    fn stats(&self, route_target_id: &RouteTargetId, window: Duration) -> Option<RouteTargetStats> {
        self.stats.lock().ok()?.get(route_target_id)?.stats(window)
    }
}

fn requires_runtime_observation(capability: &str) -> bool {
    matches!(capability, "lora")
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

async fn telemetry(client: &reqwest::Client, endpoint: &str) -> Option<TelemetryResponse> {
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
    (response.version == 2).then_some(response)
}
async fn ready(client: &reqwest::Client, endpoint: &str) -> bool {
    matches!(client.get(format!("{}/readyz",endpoint.trim_end_matches('/'))).send().await,Ok(response) if response.status().is_success())
}
