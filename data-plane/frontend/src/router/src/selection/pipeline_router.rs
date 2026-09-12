// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Connector-compatible stage selection for Aggregate, P/D, and E/P/D routes.

use std::collections::BTreeSet;
use std::sync::Arc;
use std::time::{Duration, Instant};

use foretoken_kv_indexer::{KvPrefixIndexer, NoopKvPrefixIndexer};
use foretoken_model_protocol::ModelServerRole;

use crate::inventory::supports_request;
use crate::route_target_stats::NoopRouteTargetStatsReader;
use crate::{
    RouteCandidate, RouteDecision, RouteError, RouteInventory, RouteSession,
    RouteTargetStatsReader, Router, RouterPipeline, RouterRequest, RoutingProgress, RoutingStage,
    ScoredCandidate,
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

    // Builds the immutable, rank-expanded candidate snapshot for one selection round. Dynamic
    // health, capabilities, and aggregate telemetry are captured before algorithms observe it.
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
                    admission_targets: route.admission_targets.clone(),
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

    // Runs the complete Filter-Scorer-Picker stage, validating extension-produced indexes and
    // delaying stage-specific eligibility until every candidate has been scored.
    fn select(
        &self,
        request: &RouterRequest,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut C,
        eligible: impl Fn(&RouteCandidate, &[ScoredCandidate]) -> bool,
        error: RouteError,
    ) -> Result<RouteCandidate, RouteError> {
        let started = Instant::now();
        let metrics = &crate::metrics::METRICS;
        let round = routing_progress.current_stage;
        let [filter_name, scorer_name, picker_name] = self.pipeline.algorithm_names;
        // Keep every early return inside the round so failed candidate discovery or invalid
        // algorithm output is counted as well as successful selections.
        let result = (|| {
            // Filter and Scorer see the complete compatible, healthy snapshot. Stage and connector
            // eligibility are applied after scoring and before Picker.
            let candidates = self.candidates(request);
            metrics.candidates(round, "available", candidates.len());
            let stage_started = Instant::now();
            let filtered_indexes = self.pipeline.filter.filter(
                request,
                &candidates,
                self.kv_prefix_indexer.as_ref(),
                routing_progress,
                customized_context,
            );
            metrics.stage(round, "filter", filter_name, stage_started.elapsed());
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
            metrics.candidates(round, "filtered", filtered.len());
            let stage_started = Instant::now();
            let scores = self.pipeline.scorer.score(
                request,
                &filtered,
                self.kv_prefix_indexer.as_ref(),
                routing_progress,
                customized_context,
            );
            metrics.stage(round, "scorer", scorer_name, stage_started.elapsed());
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
            metrics.candidates(round, "selectable", selectable.len());
            if selectable.is_empty() {
                return Err(error);
            }
            let stage_started = Instant::now();
            let picked = self.pipeline.picker.pick(
                request,
                &selectable,
                routing_progress,
                customized_context,
            );
            metrics.stage(round, "picker", picker_name, stage_started.elapsed());
            let picked = picked.ok_or(RouteError::EmptyPickerResult)?;
            selectable
                .get(picked.0)
                .map(|candidate| candidate.candidate.clone())
                .ok_or(RouteError::InvalidPickerIndex { index: picked.0 })
        })();
        metrics.selection(round, started.elapsed(), result.as_ref().err());
        result
    }

    fn future_stages_available(candidate: &RouteCandidate, scored: &[ScoredCandidate]) -> bool {
        candidate.future_stages().iter().all(|role| {
            scored.iter().any(|other| {
                other.candidate.role == *role
                    && other.candidate.pipeline_scope_id == candidate.pipeline_scope_id
            })
        })
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
        routing_progress: &RoutingProgress<'_>,
        context: &mut C,
    ) -> Result<RouteCandidate, RouteError> {
        self.select(
            request,
            routing_progress,
            context,
            |candidate, scored| match candidate.role {
                ModelServerRole::Aggregate => true,
                ModelServerRole::Prefill => {
                    candidate
                        .pipeline_scope_id
                        .as_ref()
                        .is_some_and(|pipeline_scope_id| {
                            !self.pipeline_scope_has_encoder(request, pipeline_scope_id)
                                && Self::future_stages_available(candidate, scored)
                        })
                }
                ModelServerRole::Encoder => {
                    candidate.pipeline_scope_id.is_some()
                        && Self::future_stages_available(candidate, scored)
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
        routing_progress: &RoutingProgress<'_>,
        context: &mut C,
    ) -> Result<RouteCandidate, RouteError> {
        let pipeline_scope_id = routing_progress
            .pipeline_scope_id
            .expect("prefill selection context has a pipeline scope");
        self.select(
            request,
            routing_progress,
            context,
            |candidate, scored| {
                candidate.role == ModelServerRole::Prefill
                    && candidate.pipeline_scope_id.as_deref() == Some(pipeline_scope_id)
                    && Self::future_stages_available(candidate, scored)
            },
            RouteError::NoMatchingRouteTarget {
                model: request.model.clone(),
            },
        )
    }

    fn select_decode_in_pipeline_scope(
        &self,
        request: &RouterRequest,
        routing_progress: &RoutingProgress<'_>,
        context: &mut C,
    ) -> Result<RouteCandidate, RouteError> {
        let pipeline_scope_id = routing_progress
            .pipeline_scope_id
            .expect("decode selection context has a pipeline scope");
        self.select(
            request,
            routing_progress,
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
    Encoder {
        pipeline_scope_id: String,
    },
    Prefill {
        pipeline_scope_id: String,
        encoder_completed: bool,
    },
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
        let routing_progress = RoutingProgress {
            current_stage: RoutingStage::Initial,
            completed_stages: &[],
            pipeline_scope_id: None,
        };
        let candidate = self.router.select_initial(
            &self.request,
            &routing_progress,
            &mut self.customized_context,
        )?;
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
                encoder_completed: false,
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
        let routing_progress = RoutingProgress {
            current_stage: RoutingStage::Prefill,
            completed_stages: &[ModelServerRole::Encoder],
            pipeline_scope_id: Some(pipeline_scope_id),
        };
        let prefill = self.router.select_prefill_in_pipeline_scope(
            &self.request,
            &routing_progress,
            &mut self.customized_context,
        )?;
        self.stage = SessionStage::Prefill {
            pipeline_scope_id: prefill
                .pipeline_scope_id
                .clone()
                .expect("eligible prefill has a pipeline scope"),
            encoder_completed: true,
        };
        Ok(prefill.decision())
    }

    fn select_decode(&mut self) -> Result<RouteDecision, RouteError> {
        // Decode selection builds a fresh healthy and telemetry snapshot rather than reusing
        // candidates observed for the earlier Prefill choice.
        let SessionStage::Prefill {
            pipeline_scope_id,
            encoder_completed,
        } = &self.stage
        else {
            return Err(RouteError::DecodeBeforePrefill);
        };
        let completed_stages: &[ModelServerRole] = if *encoder_completed {
            &[ModelServerRole::Encoder, ModelServerRole::Prefill]
        } else {
            &[ModelServerRole::Prefill]
        };
        let routing_progress = RoutingProgress {
            current_stage: RoutingStage::Decode,
            completed_stages,
            pipeline_scope_id: Some(pipeline_scope_id),
        };
        let decode = self.router.select_decode_in_pipeline_scope(
            &self.request,
            &routing_progress,
            &mut self.customized_context,
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
