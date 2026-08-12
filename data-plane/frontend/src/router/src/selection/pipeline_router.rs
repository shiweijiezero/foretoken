// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Fixed Aggregate, P/D, and E/P/D physical route-target selection.

use std::sync::Arc;

use foretoken_kv_indexer::{KvPrefixIndexer, NoopKvPrefixIndexer};
use foretoken_model_protocol::ModelServerRole;

use crate::inventory::supports_request;
use crate::{
    NoopRouteTargetStatsReader, RouteCandidate, RouteDecision, RouteError, RouteInventory,
    RouteSession, RouteTargetStatsReader, Router, RouterPipeline, RouterRequest,
};

/// Router implementation that runs one Filter-Scorer-Picker pipeline per selection round.
pub struct PipelineRouter<C: Send + 'static = ()> {
    inventory: Arc<dyn RouteInventory>,
    kv_prefix_indexer: Arc<dyn KvPrefixIndexer>,
    route_target_stats_reader: Arc<dyn RouteTargetStatsReader>,
    pipeline: Arc<RouterPipeline<C>>,
}
impl<C: Send + 'static> PipelineRouter<C> {
    /// Creates a Router with no-op KV-prefix and route-target statistics readers.
    pub fn with_pipeline(inventory: Arc<dyn RouteInventory>, pipeline: RouterPipeline<C>) -> Self {
        Self {
            inventory,
            kv_prefix_indexer: Arc::new(NoopKvPrefixIndexer),
            route_target_stats_reader: Arc::new(NoopRouteTargetStatsReader),
            pipeline: Arc::new(pipeline),
        }
    }

    /// Replaces the KV-prefix reader used by Filter and Scorer.
    pub fn with_kv_prefix_indexer(mut self, kv_prefix_indexer: Arc<dyn KvPrefixIndexer>) -> Self {
        self.kv_prefix_indexer = kv_prefix_indexer;
        self
    }

    /// Replaces the route-target statistics reader used by Filter and Scorer.
    pub fn with_route_target_stats_reader(
        mut self,
        route_target_stats_reader: Arc<dyn RouteTargetStatsReader>,
    ) -> Self {
        self.route_target_stats_reader = route_target_stats_reader;
        self
    }

    fn candidates(&self, request: &RouterRequest) -> Vec<RouteCandidate> {
        self.inventory
            .model_routes()
            .candidates(request)
            .into_iter()
            .filter(|route| {
                self.inventory
                    .is_route_target_healthy(&route.route_target_id)
            })
            .filter(|route| {
                supports_request(
                    &self
                        .inventory
                        .effective_capabilities(&route.route_target_id),
                    request,
                )
            })
            .flat_map(|route| {
                // Runtime telemetry is currently route-target aggregate only; duplicate that
                // snapshot across rank candidates rather than claiming rank-specific load.
                let load = self.inventory.route_target_load(&route.route_target_id);
                (0..route.data_parallel_size).map(move |data_parallel_rank| RouteCandidate {
                    route_target_id: route.route_target_id.clone(),
                    target: route.target.clone(),
                    role: route.role,
                    model: route.model.clone(),
                    revision: route.revision.clone(),
                    domain_id: route.domain_id.clone(),
                    data_parallel_rank,
                    route_target_load: load.clone(),
                })
            })
            .collect()
    }

    fn select(
        &self,
        request: &RouterRequest,
        customized_context: &mut C,
        eligible: impl Fn(&RouteCandidate, &[crate::ScoredCandidate]) -> bool,
        error: RouteError,
    ) -> Result<RouteCandidate, RouteError> {
        // Filter and Scorer receive the complete compatible, healthy snapshot so decisions such as
        // downstream Decode cost can compare every viable route target. Stage/domain narrowing follows
        // scoring and immediately precedes Picker, without hiding information from extensions.
        let filtered = self.pipeline.filter.filter(
            request,
            self.candidates(request),
            self.kv_prefix_indexer.as_ref(),
            self.route_target_stats_reader.as_ref(),
            customized_context,
        );
        let scored = self.pipeline.scorer.score(
            request,
            filtered,
            self.kv_prefix_indexer.as_ref(),
            self.route_target_stats_reader.as_ref(),
            customized_context,
        );
        let selectable = scored
            .iter()
            .filter(|candidate| eligible(&candidate.candidate, &scored))
            .cloned()
            .collect::<Vec<_>>();
        let picked = self
            .pipeline
            .picker
            .pick(request, &selectable, customized_context)
            .ok_or(error)?;
        // Pickers cannot create candidates or alter their identity; reject an out-of-set result.
        selectable
            .into_iter()
            .find(|candidate| candidate.candidate == picked)
            .map(|candidate| candidate.candidate)
            .ok_or(RouteError::InvalidPickerResult)
    }

    fn domain_has_encoder(&self, request: &RouterRequest, domain: &str) -> bool {
        self.inventory
            .model_routes()
            .candidates(request)
            .into_iter()
            .any(|route| {
                route.role == ModelServerRole::Encoder && route.domain_id.as_deref() == Some(domain)
            })
    }

    fn select_initial(
        &self,
        request: &RouterRequest,
        context: &mut C,
    ) -> Result<RouteCandidate, RouteError> {
        self.select(
            request,
            context,
            |candidate, scored| match candidate.role {
                ModelServerRole::Aggregate => true,
                ModelServerRole::Prefill => candidate.domain_id.as_ref().is_some_and(|domain| {
                    !self.domain_has_encoder(request, domain)
                        && scored.iter().any(|other| {
                            other.candidate.domain_id.as_ref() == Some(domain)
                                && other.candidate.role == ModelServerRole::Decode
                        })
                }),
                ModelServerRole::Encoder => candidate.domain_id.as_ref().is_some_and(|domain| {
                    scored.iter().any(|other| {
                        other.candidate.domain_id.as_ref() == Some(domain)
                            && other.candidate.role == ModelServerRole::Prefill
                    }) && scored.iter().any(|other| {
                        other.candidate.domain_id.as_ref() == Some(domain)
                            && other.candidate.role == ModelServerRole::Decode
                    })
                }),
                ModelServerRole::Decode => false,
            },
            RouteError::NoMatchingRouteTarget {
                model: request.model.clone(),
            },
        )
    }

    fn select_prefill_in_domain(
        &self,
        request: &RouterRequest,
        context: &mut C,
        domain: &str,
    ) -> Result<RouteCandidate, RouteError> {
        self.select(
            request,
            context,
            |candidate, scored| {
                candidate.role == ModelServerRole::Prefill
                    && candidate.domain_id.as_deref() == Some(domain)
                    && scored.iter().any(|other| {
                        other.candidate.role == ModelServerRole::Decode
                            && other.candidate.domain_id.as_deref() == Some(domain)
                    })
            },
            RouteError::NoMatchingRouteTarget {
                model: request.model.clone(),
            },
        )
    }

    fn select_decode_in_domain(
        &self,
        request: &RouterRequest,
        context: &mut C,
        domain: &str,
    ) -> Result<RouteCandidate, RouteError> {
        self.select(
            request,
            context,
            |candidate, _| {
                candidate.role == ModelServerRole::Decode
                    && candidate.domain_id.as_deref() == Some(domain)
            },
            RouteError::NoMatchingDecode {
                model: request.model.clone(),
            },
        )
    }
}
impl PipelineRouter<()> {
    /// Creates a Router with the default pipeline and no-op data readers.
    pub fn new(inventory: Arc<dyn RouteInventory>) -> Self {
        Self::with_pipeline(inventory, crate::RouterPipelineConfig::default().build())
    }
}

#[derive(Clone)]
enum SessionStage {
    Initial,
    Encoder { domain_id: String },
    Prefill { domain_id: String },
    Complete,
}

struct Session<C: Send + 'static> {
    router: PipelineRouter<C>,
    request: RouterRequest,
    customized_context: C,
    stage: SessionStage,
}
impl<C: Send + 'static> RouteSession for Session<C> {
    fn select_initial(&mut self) -> Result<RouteDecision, RouteError> {
        let candidate = self
            .router
            .select_initial(&self.request, &mut self.customized_context)?;
        self.stage = match candidate.role {
            ModelServerRole::Encoder => SessionStage::Encoder {
                domain_id: candidate
                    .domain_id
                    .clone()
                    .expect("eligible encoder has a domain"),
            },
            ModelServerRole::Prefill => SessionStage::Prefill {
                domain_id: candidate
                    .domain_id
                    .clone()
                    .expect("eligible prefill has a domain"),
            },
            ModelServerRole::Aggregate => SessionStage::Complete,
            ModelServerRole::Decode => unreachable!("initial eligibility rejects Decode"),
        };
        Ok(candidate.decision())
    }

    fn select_prefill(&mut self) -> Result<RouteDecision, RouteError> {
        let SessionStage::Encoder { domain_id } = &self.stage else {
            return Err(RouteError::PrefillBeforeEncoder);
        };
        let prefill = self.router.select_prefill_in_domain(
            &self.request,
            &mut self.customized_context,
            domain_id,
        )?;
        self.stage = SessionStage::Prefill {
            domain_id: prefill
                .domain_id
                .clone()
                .expect("eligible prefill has a domain"),
        };
        Ok(prefill.decision())
    }

    fn select_decode(&mut self) -> Result<RouteDecision, RouteError> {
        // Decode selection calls `select` again, intentionally reading a fresh healthy snapshot
        // rather than reusing candidates observed for the earlier Prefill choice.
        let SessionStage::Prefill { domain_id } = &self.stage else {
            return Err(RouteError::DecodeBeforePrefill);
        };
        let decode = self.router.select_decode_in_domain(
            &self.request,
            &mut self.customized_context,
            domain_id,
        )?;
        self.stage = SessionStage::Complete;
        Ok(decode.decision())
    }
}
impl<C: Send + 'static> Router for PipelineRouter<C> {
    fn start(&self, request: RouterRequest) -> Box<dyn RouteSession> {
        Box::new(Session {
            router: Self {
                inventory: self.inventory.clone(),
                kv_prefix_indexer: self.kv_prefix_indexer.clone(),
                route_target_stats_reader: self.route_target_stats_reader.clone(),
                pipeline: self.pipeline.clone(),
            },
            customized_context: (self.pipeline.customized_context_factory)(&request),
            request,
            stage: SessionStage::Initial,
        })
    }
}
