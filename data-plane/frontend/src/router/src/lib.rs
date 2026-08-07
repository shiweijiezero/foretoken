// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Policy-composable, topology-valid, capacity-safe selection of ready model components.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::sync::atomic::{AtomicUsize, Ordering};

use serde::{Deserialize, Serialize};
use thiserror::Error;

use crate::core::reservation::ReservationStore;

pub mod algorithm;
pub mod builtins;
mod core;
mod policy;

pub use algorithm::{RouteFilter, RoutePicker, RouteScorer};
pub use builtins::RouterAlgorithm;
pub use core::reservation::RouteReservation;
pub use policy::{
    RouteComponentCandidate, RouteOptionCandidate, RouteOptionKind, RouteScore, RouterPolicy,
    ScoredRouteOption,
};

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
#[serde(transparent)]
pub struct BackendId(String);
impl BackendId {
    pub fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// Complete, read-only routing view over one canonical vLLM request.
///
/// Model identity and northbound capabilities remain outside `GenerateRequest` because one
/// vLLM engine normally binds them at startup. Execution fields stay borrowed from the original
/// request so routing never clones, mutates, or consumes it.
#[derive(Clone, Copy)]
pub struct RouteContext<'a> {
    pub model: &'a str,
    pub revision: Option<&'a str>,
    pub required_capabilities: &'a BTreeSet<String>,
    pub request: &'a vllm_llm::GenerateRequest,
}

impl RouteContext<'_> {
    pub fn prompt_token_ids(&self) -> &[u32] {
        &self.request.prompt_token_ids
    }

    pub fn token_count(&self) -> usize {
        self.request.prompt_token_ids.len()
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum BackendRole {
    #[default]
    Aggregate,
    Encoder,
    Prefill,
    Decode,
}

/// One independently healthy/capacity-accounted execution component.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct BackendRoute {
    pub backend_id: BackendId,
    pub model: String,
    pub revision: String,
    #[serde(default)]
    pub capabilities: BTreeSet<String>,
    pub max_input_tokens: Option<usize>,
    pub ready: bool,
    #[serde(default)]
    pub role: BackendRole,
    #[serde(default)]
    pub domain_id: Option<String>,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteDecision {
    pub backend_id: BackendId,
    pub model: String,
    pub revision: String,
}

/// The public plan keeps telemetry and scores private while preserving the minimal decision contract.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ExecutionPlan {
    Aggregate {
        decision: RouteDecision,
    },
    PrefillDecode {
        prefill: RouteDecision,
        decode: RouteDecision,
    },
    EncoderPrefillDecode {
        encoder: RouteDecision,
        prefill: RouteDecision,
        decode: RouteDecision,
    },
}
impl ExecutionPlan {
    pub fn decision(&self) -> &RouteDecision {
        match self {
            Self::Aggregate { decision } => decision,
            Self::PrefillDecode { decode, .. } | Self::EncoderPrefillDecode { decode, .. } => {
                decode
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ComponentCapacity {
    pub component_id: BackendId,
    pub running_requests: u64,
    pub max_concurrent_requests: u64,
}
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AdmissionTarget {
    pub components: Vec<ComponentCapacity>,
}
pub struct RouteSelection {
    pub decision: RouteDecision,
    pub plan: ExecutionPlan,
    pub reservation: RouteReservation,
}

mod sealed {
    pub trait Router {}
}

/// Core execution boundary. Users customize `RouterPolicy`; topology and reservation are sealed.
pub trait Router: sealed::Router + Send + Sync {
    fn select(&self, context: RouteContext<'_>) -> Result<RouteSelection, RouteError>;
}
pub trait RouteInventory: Send + Sync {
    fn model_router(&self) -> &ModelRouter;
    fn is_backend_healthy(&self, backend_id: &BackendId) -> bool;
    fn admission_target(&self, backend_id: &BackendId) -> Option<AdmissionTarget>;

    /// Runtime-observed capabilities bounded by the route's static policy.
    fn effective_capabilities(&self, backend_id: &BackendId) -> BTreeSet<String> {
        self.model_router()
            .routes()
            .iter()
            .find(|route| &route.backend_id == backend_id)
            .map(|route| route.capabilities.clone())
            .unwrap_or_default()
    }
}
pub trait KvPrefixScorer: Send + Sync {
    fn score_prefill_prefix(&self, backend_id: &BackendId, context: RouteContext<'_>) -> usize;
}
pub struct NeutralScorer;
impl KvPrefixScorer for NeutralScorer {
    fn score_prefill_prefix(&self, _: &BackendId, _: RouteContext<'_>) -> usize {
        0
    }
}
#[derive(Debug, Error, PartialEq, Eq)]
pub enum RouteError {
    #[error("no ready backend matches model {model}")]
    NoMatchingBackend { model: String },
    #[error("capacity telemetry is unavailable for model {model}")]
    TelemetryUnavailable { model: String },
    #[error("no capacity is available for model {model}")]
    NoCapacity { model: String },
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ModelRouter {
    routes: Vec<BackendRoute>,
}
impl ModelRouter {
    pub fn new(mut routes: Vec<BackendRoute>) -> Self {
        routes.sort_by(|a, b| a.backend_id.cmp(&b.backend_id));
        Self { routes }
    }
    pub fn routes(&self) -> &[BackendRoute] {
        &self.routes
    }
    pub fn candidates(&self, context: RouteContext<'_>) -> Vec<&BackendRoute> {
        self.candidates_without_capabilities(context)
            .into_iter()
            .filter(|route| {
                context.required_capabilities.is_subset(&route.capabilities)
                    && (context.request.lora_request.is_none()
                        || route.capabilities.contains("lora"))
                    && (context.request.mm_features.is_none()
                        || route.capabilities.contains("multimodal"))
                    && (context.request.reasoning_parser_kwargs.is_none()
                        || route.capabilities.contains("reasoning"))
            })
            .collect()
    }

    pub fn candidates_without_capabilities(&self, context: RouteContext<'_>) -> Vec<&BackendRoute> {
        self.routes
            .iter()
            .filter(|route| {
                route.ready
                    && route.model == context.model
                    && context
                        .revision
                        .is_none_or(|revision| route.revision == revision)
                    && route
                        .max_input_tokens
                        .is_none_or(|limit| context.token_count() <= limit)
            })
            .collect()
    }
}

fn decision(route: &BackendRoute) -> RouteDecision {
    RouteDecision {
        backend_id: route.backend_id.clone(),
        model: route.model.clone(),
        revision: route.revision.clone(),
    }
}

pub struct PolicyRouter {
    inventory: Arc<dyn RouteInventory>,
    policy: RouterPolicy,
    round_robin: AtomicUsize,
    reservations: Arc<ReservationStore>,
}
struct ScoredOption {
    plan: ExecutionPlan,
    targets: Vec<AdmissionTarget>,
    policy: ScoredRouteOption,
}
impl PolicyRouter {
    pub fn with_policy(inventory: Arc<dyn RouteInventory>, policy: RouterPolicy) -> Self {
        Self {
            inventory,
            policy,
            round_robin: AtomicUsize::new(0),
            reservations: Arc::new(ReservationStore::default()),
        }
    }
    fn eligible(&self, context: RouteContext<'_>) -> Vec<BackendRoute> {
        self.inventory
            .model_router()
            .candidates_without_capabilities(context)
            .into_iter()
            .filter(|route| self.inventory.is_backend_healthy(&route.backend_id))
            .filter(|route| {
                let capabilities = self.inventory.effective_capabilities(&route.backend_id);
                context.required_capabilities.is_subset(&capabilities)
                    && (context.request.lora_request.is_none() || capabilities.contains("lora"))
                    && (context.request.mm_features.is_none()
                        || capabilities.contains("multimodal"))
                    && (context.request.reasoning_parser_kwargs.is_none()
                        || capabilities.contains("reasoning"))
            })
            .cloned()
            .collect()
    }
    fn component(
        &self,
        route: &BackendRoute,
        uses_prefix_locality: bool,
    ) -> Option<(RouteComponentCandidate, AdmissionTarget)> {
        let target = self.inventory.admission_target(&route.backend_id)?;
        let load = self.reservations.available_load(&target)?;
        Some((
            RouteComponentCandidate {
                backend_id: route.backend_id.clone(),
                role: route.role,
                domain_id: route.domain_id.clone(),
                load,
                uses_prefix_locality,
            },
            target,
        ))
    }

    fn option(
        &self,
        kind: RouteOptionKind,
        plan: ExecutionPlan,
        routes: &[(&BackendRoute, bool)],
        context: RouteContext<'_>,
    ) -> Option<ScoredOption> {
        let mut components = Vec::with_capacity(routes.len());
        let mut targets = Vec::with_capacity(routes.len());
        for (route, uses_prefix_locality) in routes {
            let (component, target) = self.component(route, *uses_prefix_locality)?;
            components.push(component);
            targets.push(target);
        }
        let option = RouteOptionCandidate { kind, components };
        if !self.policy.filter.allows(&option, context) {
            return None;
        }
        let score = self.policy.scorer.score(&option, context);
        Some(ScoredOption {
            plan,
            targets,
            policy: ScoredRouteOption { option, score },
        })
    }
    fn reserve(&self, targets: &[&AdmissionTarget]) -> Option<RouteReservation> {
        self.reservations.try_reserve(targets)
    }
    fn ranked(
        &self,
        values: Vec<ScoredOption>,
        context: RouteContext<'_>,
        next: usize,
    ) -> Vec<ScoredOption> {
        let candidates = values
            .iter()
            .map(|value| value.policy.clone())
            .collect::<Vec<_>>();
        let mut remaining = values.into_iter().map(Some).collect::<Vec<_>>();
        let mut ranked = Vec::with_capacity(remaining.len());
        for index in self.policy.picker.order(&candidates, context, next) {
            if let Some(candidate) = remaining.get_mut(index).and_then(Option::take) {
                ranked.push(candidate);
            }
        }
        // A picker controls preference only. Core appends omissions so exclusion remains the
        // filter's responsibility and malformed output cannot erase otherwise valid capacity.
        ranked.extend(remaining.into_iter().flatten());
        ranked
    }
    fn selection_error(
        &self,
        context: RouteContext<'_>,
        candidates: &[BackendRoute],
    ) -> RouteError {
        if candidates
            .iter()
            .all(|route| self.inventory.admission_target(&route.backend_id).is_none())
        {
            RouteError::TelemetryUnavailable {
                model: context.model.to_owned(),
            }
        } else {
            RouteError::NoCapacity {
                model: context.model.to_owned(),
            }
        }
    }
}
impl sealed::Router for PolicyRouter {}

impl Router for PolicyRouter {
    fn select(&self, context: RouteContext<'_>) -> Result<RouteSelection, RouteError> {
        let candidates = self.eligible(context);
        if candidates.is_empty() {
            return Err(RouteError::NoMatchingBackend {
                model: context.model.to_owned(),
            });
        }

        let mut options = Vec::new();
        for route in candidates
            .iter()
            .filter(|route| route.role == BackendRole::Aggregate)
        {
            let route_decision = decision(route);
            if let Some(option) = self.option(
                RouteOptionKind::Aggregate,
                ExecutionPlan::Aggregate {
                    decision: route_decision,
                },
                &[(route, false)],
                context,
            ) {
                options.push(option);
            }
        }

        for encoder in candidates
            .iter()
            .filter(|route| route.role == BackendRole::Encoder)
        {
            let Some(domain) = encoder.domain_id.as_ref() else {
                continue;
            };
            for prefill in candidates.iter().filter(|route| {
                route.role == BackendRole::Prefill && route.domain_id.as_ref() == Some(domain)
            }) {
                for decode in candidates.iter().filter(|route| {
                    route.role == BackendRole::Decode && route.domain_id.as_ref() == Some(domain)
                }) {
                    if let Some(option) = self.option(
                        RouteOptionKind::EncoderPrefillDecode,
                        ExecutionPlan::EncoderPrefillDecode {
                            encoder: decision(encoder),
                            prefill: decision(prefill),
                            decode: decision(decode),
                        },
                        &[(encoder, false), (prefill, true), (decode, false)],
                        context,
                    ) {
                        options.push(option);
                    }
                }
            }
        }

        let epd_domains = candidates
            .iter()
            .filter(|route| route.role == BackendRole::Encoder)
            .filter_map(|route| route.domain_id.as_deref())
            .collect::<BTreeSet<_>>();
        for prefill in candidates
            .iter()
            .filter(|route| route.role == BackendRole::Prefill)
        {
            let Some(domain) = prefill.domain_id.as_ref() else {
                continue;
            };
            if epd_domains.contains(domain.as_str()) {
                continue;
            }
            for decode in candidates.iter().filter(|route| {
                route.role == BackendRole::Decode && route.domain_id.as_ref() == Some(domain)
            }) {
                if let Some(option) = self.option(
                    RouteOptionKind::PrefillDecode,
                    ExecutionPlan::PrefillDecode {
                        prefill: decision(prefill),
                        decode: decision(decode),
                    },
                    &[(prefill, true), (decode, false)],
                    context,
                ) {
                    options.push(option);
                }
            }
        }

        let next = self.round_robin.fetch_add(1, Ordering::Relaxed);
        for option in self.ranked(options, context, next) {
            let targets = option.targets.iter().collect::<Vec<_>>();
            if let Some(reservation) = self.reserve(&targets) {
                let decision = option.plan.decision().clone();
                return Ok(RouteSelection {
                    decision,
                    plan: option.plan,
                    reservation,
                });
            }
        }
        Err(self.selection_error(context, &candidates))
    }
}
#[cfg(test)]
#[path = "tests/model_router.rs"]
mod tests;
