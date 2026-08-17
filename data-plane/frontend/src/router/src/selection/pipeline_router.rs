// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Fixed physical route selection for Aggregate, P/D, and E/P/D route sets.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::Duration;

use foretoken_kv_indexer::{KvPrefixIndexer, NoopKvPrefixIndexer};
use foretoken_model_protocol::ModelServerRole;

use crate::inventory::supports_request;
use crate::{
    NoopRouteTargetStatsReader, RouteCandidate, RouteDecision, RouteError, RouteInventory,
    RouteSession, RouteTargetStatsReader, Router, RouterPipeline, RouterRequest, ScoredCandidate,
};

/// Observation window used for every route target in one routing round.
const ROUTE_TARGET_STATS_WINDOW: Duration = Duration::from_secs(60);

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

    /// Replaces the Router-owned reader used to construct candidate observations.
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
                // Statistics are route-target aggregate telemetry. Read once with the core-owned
                // window, then share the same immutable observation across all rank candidates.
                let stats = self
                    .route_target_stats_reader
                    .stats(&route.route_target_id, ROUTE_TARGET_STATS_WINDOW)
                    .map(Arc::new);
                (0..route.data_parallel_size).map(move |data_parallel_rank| RouteCandidate {
                    route_target_id: route.route_target_id.clone(),
                    target: route.target.clone(),
                    role: route.role,
                    model: route.model.clone(),
                    revision: route.revision.clone(),
                    pipeline_scope_id: route.pipeline_scope_id.clone(),
                    data_parallel_rank,
                    route_target_stats: stats.clone(),
                })
            })
            .collect()
    }

    fn select(
        &self,
        request: &RouterRequest,
        customized_context: &mut C,
        eligible: impl Fn(&RouteCandidate, &[ScoredCandidate]) -> bool,
        error: RouteError,
    ) -> Result<RouteCandidate, RouteError> {
        // Filter and Scorer receive the complete compatible, healthy snapshot so decisions such as
        // downstream Decode cost can compare every viable route target. Stage/pipeline-scope narrowing follows
        // scoring and immediately precedes Picker, without hiding information from extensions.
        let candidates = self.candidates(request);
        let filtered_indexes = self.pipeline.filter.filter(
            request,
            &candidates,
            self.kv_prefix_indexer.as_ref(),
            customized_context,
        );
        let mut seen_indexes = BTreeSet::new();
        let filtered = filtered_indexes
            .into_iter()
            .map(|index| {
                if !seen_indexes.insert(index) {
                    return Err(RouteError::DuplicateFilterIndex { index: index.0 });
                }
                candidates
                    .get(index.0)
                    .cloned()
                    .ok_or(RouteError::InvalidFilterIndex { index: index.0 })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let scores = self.pipeline.scorer.score(
            request,
            &filtered,
            self.kv_prefix_indexer.as_ref(),
            customized_context,
        );
        if scores.len() != filtered.len() {
            return Err(RouteError::InvalidScorerResult {
                expected: filtered.len(),
                actual: scores.len(),
            });
        }
        let scored = filtered
            .into_iter()
            .zip(scores)
            .map(|(candidate, score)| ScoredCandidate { candidate, score })
            .collect::<Vec<_>>();
        let selectable = scored
            .iter()
            .filter(|candidate| eligible(&candidate.candidate, &scored))
            .cloned()
            .collect::<Vec<_>>();
        if selectable.is_empty() {
            return Err(error);
        }
        let picked = self
            .pipeline
            .picker
            .pick(request, &selectable, customized_context)
            .ok_or(RouteError::EmptyPickerResult)?;
        selectable
            .get(picked.0)
            .map(|candidate| candidate.candidate.clone())
            .ok_or(RouteError::InvalidPickerIndex { index: picked.0 })
    }

    fn pipeline_scope_has_encoder(&self, request: &RouterRequest, pipeline_scope_id: &str) -> bool {
        self.inventory
            .model_routes()
            .candidates(request)
            .into_iter()
            .any(|route| {
                route.role == ModelServerRole::Encoder
                    && route.pipeline_scope_id.as_deref() == Some(pipeline_scope_id)
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
                ModelServerRole::Prefill => {
                    candidate
                        .pipeline_scope_id
                        .as_ref()
                        .is_some_and(|pipeline_scope_id| {
                            !self.pipeline_scope_has_encoder(request, pipeline_scope_id)
                                && scored.iter().any(|other| {
                                    other.candidate.pipeline_scope_id.as_ref()
                                        == Some(pipeline_scope_id)
                                        && other.candidate.role == ModelServerRole::Decode
                                })
                        })
                }
                ModelServerRole::Encoder => {
                    candidate
                        .pipeline_scope_id
                        .as_ref()
                        .is_some_and(|pipeline_scope_id| {
                            scored.iter().any(|other| {
                                other.candidate.pipeline_scope_id.as_ref()
                                    == Some(pipeline_scope_id)
                                    && other.candidate.role == ModelServerRole::Prefill
                            }) && scored.iter().any(|other| {
                                other.candidate.pipeline_scope_id.as_ref()
                                    == Some(pipeline_scope_id)
                                    && other.candidate.role == ModelServerRole::Decode
                            })
                        })
                }
                ModelServerRole::Decode => false,
            },
            RouteError::NoMatchingRouteTarget {
                model: request.model.clone(),
            },
        )
    }

    fn select_prefill_in_pipeline_scope(
        &self,
        request: &RouterRequest,
        context: &mut C,
        pipeline_scope_id: &str,
    ) -> Result<RouteCandidate, RouteError> {
        self.select(
            request,
            context,
            |candidate, scored| {
                candidate.role == ModelServerRole::Prefill
                    && candidate.pipeline_scope_id.as_deref() == Some(pipeline_scope_id)
                    && scored.iter().any(|other| {
                        other.candidate.role == ModelServerRole::Decode
                            && other.candidate.pipeline_scope_id.as_deref()
                                == Some(pipeline_scope_id)
                    })
            },
            RouteError::NoMatchingRouteTarget {
                model: request.model.clone(),
            },
        )
    }

    fn select_decode_in_pipeline_scope(
        &self,
        request: &RouterRequest,
        context: &mut C,
        pipeline_scope_id: &str,
    ) -> Result<RouteCandidate, RouteError> {
        self.select(
            request,
            context,
            |candidate, _| {
                candidate.role == ModelServerRole::Decode
                    && candidate.pipeline_scope_id.as_deref() == Some(pipeline_scope_id)
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
        Self::with_pipeline(
            inventory,
            crate::RouterPipelineConfig::default()
                .build()
                .expect("built-in Router pipeline configuration must be valid"),
        )
    }
}

#[derive(Clone)]
enum SessionStage {
    Initial,
    Encoder { pipeline_scope_id: String },
    Prefill { pipeline_scope_id: String },
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
                pipeline_scope_id: candidate
                    .pipeline_scope_id
                    .clone()
                    .expect("eligible encoder has a pipeline scope"),
            },
            ModelServerRole::Prefill => SessionStage::Prefill {
                pipeline_scope_id: candidate
                    .pipeline_scope_id
                    .clone()
                    .expect("eligible prefill has a pipeline scope"),
            },
            ModelServerRole::Aggregate => SessionStage::Complete,
            ModelServerRole::Decode => unreachable!("initial eligibility rejects Decode"),
        };
        Ok(candidate.decision())
    }

    fn select_prefill(&mut self) -> Result<RouteDecision, RouteError> {
        let SessionStage::Encoder { pipeline_scope_id } = &self.stage else {
            return Err(RouteError::PrefillBeforeEncoder);
        };
        let prefill = self.router.select_prefill_in_pipeline_scope(
            &self.request,
            &mut self.customized_context,
            pipeline_scope_id,
        )?;
        self.stage = SessionStage::Prefill {
            pipeline_scope_id: prefill
                .pipeline_scope_id
                .clone()
                .expect("eligible prefill has a pipeline scope"),
        };
        Ok(prefill.decision())
    }

    fn select_decode(&mut self) -> Result<RouteDecision, RouteError> {
        // Decode selection builds a fresh healthy and telemetry snapshot rather than reusing
        // candidates observed for the earlier Prefill choice.
        let SessionStage::Prefill { pipeline_scope_id } = &self.stage else {
            return Err(RouteError::DecodeBeforePrefill);
        };
        let decode = self.router.select_decode_in_pipeline_scope(
            &self.request,
            &mut self.customized_context,
            pipeline_scope_id,
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
