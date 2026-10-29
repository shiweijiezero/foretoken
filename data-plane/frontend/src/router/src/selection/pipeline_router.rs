// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Connector-compatible stage selection for Aggregate, P/D, and E/P/D routes.

use std::collections::{BTreeMap, BTreeSet};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use foretoken_kv_indexer::{
    KvPrefixIndexer, KvPrefixQueryResult, KvPrefixReuseScope, NoopKvPrefixIndexer,
};
use foretoken_model_protocol::ModelServerRole;

use crate::inventory::supports_request;
use crate::route_target_stats::NoopRouteTargetStatsReader;
use crate::routing_load::{ReservationKey, RoutingReservations};
use crate::{
    RouteCandidate, RouteDecision, RouteError, RouteInventory, RouteSession,
    RouteTargetStatsReader, Router, RouterPipeline, RouterPipelines, RouterRequest,
    RoutingProgress, RoutingStage, ScoredCandidate,
};

/// Observation window used for every route target in one routing round.
const ROUTE_TARGET_STATS_WINDOW: Duration = Duration::from_secs(60);

/// Router implementation that runs one Filter-Scorer-Picker pipeline per selection round.
pub struct PipelineRouter<C: Send + 'static = ()> {
    inventory: Arc<dyn RouteInventory>,
    snapshot_version: u64,
    kv_prefix_indexer: Arc<dyn KvPrefixIndexer>,
    route_target_stats_reader: Arc<dyn RouteTargetStatsReader>,
    pipelines: Arc<RouterPipelines<C>>,
    routing_load: Arc<Mutex<RoutingReservations>>,
    metrics: Arc<crate::metrics::RouterMetricsScope>,
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
    selected: Vec<ReservationKey>,
    track_output_blocks: bool,
}

impl<C: Send + 'static> PipelineRouter<C> {
    /// Creates a Router with no-op KV-prefix and route-target statistics readers.
    pub fn with_pipeline(
        inventory: Arc<dyn RouteInventory>,
        pipeline: impl Into<Arc<RouterPipeline<C>>>,
    ) -> Self {
        Self::with_pipelines(inventory, Arc::new(RouterPipelines::new(pipeline)))
    }

    /// Creates a Router from one atomically prepared set of model and execution-role algorithms.
    pub fn with_pipelines(
        inventory: Arc<dyn RouteInventory>,
        pipelines: Arc<RouterPipelines<C>>,
    ) -> Self {
        let metrics = Arc::new(crate::metrics::RouterMetricsScope::new(
            inventory.as_ref(),
            |model, round| {
                let role = inventory.model_routes().selection_role(model, round);
                pipelines.get(model, role).algorithm_names
            },
        ));
        Self {
            inventory,
            snapshot_version: 0,
            kv_prefix_indexer: Arc::new(NoopKvPrefixIndexer),
            route_target_stats_reader: Arc::new(NoopRouteTargetStatsReader),
            pipelines,
            routing_load: Arc::new(Mutex::new(RoutingReservations::default())),
            metrics,
        }
    }

    /// Associates this inventory with its serving-snapshot version for shared algorithm state.
    /// RuntimeBuilder supplies the version; sessions retain it with their original inventory.
    pub fn with_snapshot_version(mut self, version: u64) -> Self {
        self.snapshot_version = version;
        self
    }

    /// Shares frontend-owned request load across runtime generations built by RuntimeBuilder.
    pub fn with_load_state(mut self, state: crate::RoutingLoadState) -> Self {
        self.routing_load = state.0;
        self
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

#[async_trait::async_trait]
impl<C: Send + 'static> Router for PipelineRouter<C> {
    async fn start(&self, request: RouterRequest) -> Box<dyn RouteSession> {
        let needs_prefix = self
            .inventory
            .model_routes()
            .routes()
            .iter()
            .filter(|route| route.model == request.model)
            .any(|route| {
                let pipeline = self.pipelines.get(&request.model, route.role);
                pipeline.filter.needs_kv_prefix() || pipeline.scorer.needs_kv_prefix()
            });
        let reuse_scopes = self.kv_reuse_scopes(&request);
        let kv_prefix_indexer = if needs_prefix || !reuse_scopes.is_empty() {
            let candidates = {
                let reservations = self
                    .routing_load
                    .lock()
                    .expect("routing load lock poisoned");
                self.candidates(&request, &reservations, false)
            };
            let lookups = candidates
                .iter()
                .filter(|candidate| {
                    if needs_prefix {
                        matches!(
                            candidate.role,
                            ModelServerRole::Aggregate
                                | ModelServerRole::Prefill
                                | ModelServerRole::Decode
                        )
                    } else {
                        candidate.role == ModelServerRole::Prefill
                            && reuse_scopes.iter().any(|scope| {
                                scope
                                    .prefill_route_target_ids
                                    .contains(&candidate.route_target_id.as_str())
                            })
                    }
                })
                .filter_map(|candidate| {
                    request
                        .kv_prefix_lookup(
                            candidate.route_target_id.as_str(),
                            candidate.data_parallel_rank,
                        )
                        .ok()
                })
                .collect::<Vec<_>>();
            self.kv_prefix_indexer
                .prepare(&lookups, &reuse_scopes)
                .await
                .unwrap_or_else(|| self.kv_prefix_indexer.clone())
        } else {
            self.kv_prefix_indexer.clone()
        };
        Box::new(Session {
            router: Self {
                inventory: self.inventory.clone(),
                snapshot_version: self.snapshot_version,
                kv_prefix_indexer,
                route_target_stats_reader: self.route_target_stats_reader.clone(),
                pipelines: self.pipelines.clone(),
                routing_load: self.routing_load.clone(),
                metrics: self.metrics.clone(),
            },
            customized_context: (self
                .pipeline(&request.model, RoutingStage::Initial)
                .customized_context_factory)(&request),
            request,
            stage: SessionStage::Initial,
            selected: Vec::new(),
            track_output_blocks: false,
        })
    }
}

impl<C: Send + 'static> RouteSession for Session<C> {
    fn response_started(&mut self) {
        let mut reservations = self
            .router
            .routing_load
            .lock()
            .expect("routing load lock poisoned");
        for key in &self.selected {
            reservations.release_prompt_load(key, self.request.request_id());
        }
    }

    fn output_tokens_generated(&mut self, count: usize) {
        if !self.track_output_blocks || count == 0 {
            return;
        }
        let mut reservations = self
            .router
            .routing_load
            .lock()
            .expect("routing load lock poisoned");
        for key in &self.selected {
            reservations.record_output_tokens(key, self.request.request_id(), count);
        }
    }

    fn stage_complete(&mut self) {
        self.track_output_blocks = false;
        if self.selected.is_empty() {
            return;
        }
        let mut reservations = self
            .router
            .routing_load
            .lock()
            .expect("routing load lock poisoned");
        for key in self.selected.drain(..) {
            reservations.release(&key, self.request.request_id());
        }
    }

    fn select_initial(&mut self) -> Result<RouteDecision, RouteError> {
        let routing_progress = RoutingProgress {
            snapshot_version: self.router.snapshot_version,
            current_stage: RoutingStage::Initial,
            completed_stages: &[],
            pipeline_scope_id: None,
        };
        let candidate = self.router.select_initial(
            &self.request,
            &routing_progress,
            &mut self.customized_context,
        )?;
        self.track_output_blocks = self
            .router
            .pipeline(&self.request.model, RoutingStage::Initial)
            .scorer
            .tracks_output_blocks();
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
        self.selected.push((
            candidate.route_target_id.clone(),
            candidate.data_parallel_rank,
        ));
        Ok(self.router.decision(&self.request, &candidate))
    }

    fn select_prefill(&mut self) -> Result<RouteDecision, RouteError> {
        let SessionStage::Encoder { pipeline_scope_id } = &self.stage else {
            return Err(RouteError::PrefillBeforeEncoder);
        };
        let routing_progress = RoutingProgress {
            snapshot_version: self.router.snapshot_version,
            current_stage: RoutingStage::Prefill,
            completed_stages: &[ModelServerRole::Encoder],
            pipeline_scope_id: Some(pipeline_scope_id),
        };
        let prefill = self.router.select_prefill_in_pipeline_scope(
            &self.request,
            &routing_progress,
            &mut self.customized_context,
        )?;
        self.track_output_blocks = self
            .router
            .pipeline(&self.request.model, RoutingStage::Prefill)
            .scorer
            .tracks_output_blocks();
        self.stage = SessionStage::Prefill {
            pipeline_scope_id: prefill
                .pipeline_scope_id
                .clone()
                .expect("eligible prefill has a pipeline scope"),
            encoder_completed: true,
        };
        self.selected
            .push((prefill.route_target_id.clone(), prefill.data_parallel_rank));
        Ok(self.router.decision(&self.request, &prefill))
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
            snapshot_version: self.router.snapshot_version,
            current_stage: RoutingStage::Decode,
            completed_stages,
            pipeline_scope_id: Some(pipeline_scope_id),
        };
        let decode = self.router.select_decode_in_pipeline_scope(
            &self.request,
            &routing_progress,
            &mut self.customized_context,
        )?;
        self.track_output_blocks = self
            .router
            .pipeline(&self.request.model, RoutingStage::Decode)
            .scorer
            .tracks_output_blocks();
        self.stage = SessionStage::Complete;
        self.selected
            .push((decode.route_target_id.clone(), decode.data_parallel_rank));
        Ok(decode.decision())
    }
}

impl<C: Send + 'static> Drop for Session<C> {
    fn drop(&mut self) {
        self.stage_complete();
    }
}

impl<C: Send + 'static> PipelineRouter<C> {
    /// Resolves the current execution role independently of which candidate wins selection.
    fn pipeline(&self, model: &str, round: RoutingStage) -> &Arc<RouterPipeline<C>> {
        let role = self.inventory.model_routes().selection_role(model, round);
        self.pipelines.get(model, role)
    }

    /// Builds one rank-expanded observation snapshot before the selection algorithms run.
    /// Dynamic health, capabilities, telemetry, and frontend reservations are captured together.
    fn candidates(
        &self,
        request: &RouterRequest,
        reservations: &RoutingReservations,
        include_active_sequences: bool,
    ) -> Vec<RouteCandidate> {
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
                    // In E/P/D, Encoder owns media encoding; P/D consume its result and KV.
                    !matches!(
                        route.role,
                        ModelServerRole::Prefill | ModelServerRole::Decode
                    ) || !route
                        .pipeline_scope_id
                        .as_deref()
                        .is_some_and(|scope| self.pipeline_scope_has_encoder(request, scope)),
                )
            })
            .flat_map(|route| {
                let kv_block_size = self.inventory.kv_block_size(&route.route_target_id);
                let prefix_cache_enabled =
                    self.inventory.prefix_cache_enabled(&route.route_target_id);
                // Read the group's aggregate and rank-local gauges once, then share the immutable
                // observation across candidates; scorers select the candidate's exact DP rank.
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
                    kv_block_size,
                    prefix_cache_enabled,
                    route_target_stats: stats.clone(),
                    local_load: reservations
                        .snapshot(&(route.route_target_id.clone(), data_parallel_rank)),
                    active_sequences: if include_active_sequences {
                        reservations
                            .active_sequences(&(route.route_target_id.clone(), data_parallel_rank))
                    } else {
                        Vec::new()
                    },
                    stage_eligible: false,
                })
            })
            .collect()
    }

    /// Groups compatible Prefill and Decode routes from observed transfer settings.
    fn kv_reuse_scopes(&self, request: &RouterRequest) -> Vec<KvPrefixReuseScope<'_>> {
        let mut scopes = BTreeMap::<_, (BTreeSet<_>, BTreeSet<_>)>::new();
        for route in self
            .inventory
            .model_routes()
            .routes()
            .iter()
            .filter(|route| {
                route.ready
                    && route.model == request.model
                    && matches!(
                        route.role,
                        ModelServerRole::Prefill | ModelServerRole::Decode
                    )
                    && (request.allowed_pools.is_empty()
                        || request.allowed_pools.contains(&route.target.name))
                    && self
                        .inventory
                        .is_route_target_healthy(&route.route_target_id)
            })
        {
            let Some(pipeline_scope_id) = route.pipeline_scope_id.as_deref() else {
                continue;
            };
            let Some(transfer) = self.inventory.kv_transfer(&route.route_target_id) else {
                continue;
            };
            if !transfer.bidirectional_kv_xfer {
                continue;
            }
            let roles = scopes
                .entry((
                    route.target.service_uid.as_str(),
                    route.model.as_str(),
                    route.revision.as_str(),
                    pipeline_scope_id,
                    transfer.connector.as_str(),
                ))
                .or_default();
            match route.role {
                ModelServerRole::Prefill => {
                    roles.0.insert(route.route_target_id.as_str());
                }
                ModelServerRole::Decode => {
                    roles.1.insert(route.route_target_id.as_str());
                }
                ModelServerRole::Aggregate | ModelServerRole::Encoder => unreachable!(),
            }
        }
        scopes
            .into_values()
            .filter(|(prefills, decodes)| !prefills.is_empty() && !decodes.is_empty())
            .map(|(prefills, decodes)| KvPrefixReuseScope {
                prefill_route_target_ids: prefills.into_iter().collect(),
                decode_route_target_ids: decodes.into_iter().collect(),
            })
            .collect()
    }

    /// Builds a stage decision with an optional request-local source for Prefill.
    fn decision(&self, request: &RouterRequest, candidate: &RouteCandidate) -> RouteDecision {
        let mut decision = candidate.decision();
        if candidate.role != ModelServerRole::Prefill {
            return decision;
        }
        if let Ok(lookup) = request.kv_prefix_lookup(
            candidate.route_target_id.as_str(),
            candidate.data_parallel_rank,
        ) && let KvPrefixQueryResult::Matches(matches) =
            self.kv_prefix_indexer.prefix_matches(lookup)
        {
            decision.kv_source = matches
                .into_iter()
                .filter_map(|hit| hit.kv_source)
                .max_by_key(|source| source.matched_tokens);
        }
        decision
    }

    /// Runs Filter-Scorer-Picker and reserves the winner under one routing-load lock.
    /// Extension indexes and stage eligibility are checked before a candidate reaches Picker.
    fn select(
        &self,
        request: &RouterRequest,
        routing_progress: &RoutingProgress<'_>,
        customized_context: &mut C,
        eligible: impl Fn(&RouteCandidate, &[RouteCandidate]) -> bool,
        error: RouteError,
    ) -> Result<RouteCandidate, RouteError> {
        let started = Instant::now();
        let metrics = &self.metrics;
        let round = routing_progress.current_stage;
        let pipeline = self.pipeline(&request.model, round);
        let [filter_name, scorer_name, picker_name] = pipeline.algorithm_names;
        // Keep every early return inside the round so failed candidate discovery or invalid
        // algorithm output is counted as well as successful selections.
        let result = (|| {
            // Filter sees the complete compatible, healthy snapshot. Scorer sees all filtered
            // candidates with stage and connector eligibility marked; Picker sees only eligible ones.
            // Snapshot, scoring, and reservation share one lock so concurrent selections see load.
            let mut reservations = self
                .routing_load
                .lock()
                .expect("routing load lock poisoned");
            let candidates = self.candidates(
                request,
                &reservations,
                pipeline.scorer.needs_active_sequences(),
            );
            metrics.candidates(&request.model, round, "available", candidates.len());
            if !candidates
                .iter()
                .any(|candidate| eligible(candidate, &candidates))
            {
                return Err(error);
            }
            let stage_started = Instant::now();
            let filtered_indexes = pipeline.filter.filter(
                request,
                &candidates,
                self.kv_prefix_indexer.as_ref(),
                routing_progress,
                customized_context,
            );
            metrics.stage(
                &request.model,
                round,
                "filter",
                filter_name,
                stage_started.elapsed(),
            );
            let mut seen_indexes = BTreeSet::new();
            let mut filtered = filtered_indexes
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
            let eligibility = filtered
                .iter()
                .map(|candidate| eligible(candidate, &filtered))
                .collect::<Vec<_>>();
            for (candidate, eligible) in filtered.iter_mut().zip(eligibility) {
                candidate.stage_eligible = eligible;
            }
            metrics.candidates(&request.model, round, "filtered", filtered.len());
            let stage_started = Instant::now();
            let crate::algorithm::ScoringOutcome {
                scores,
                on_selected,
            } = pipeline.scorer.score_for_selection(
                request,
                &filtered,
                self.kv_prefix_indexer.as_ref(),
                routing_progress,
                customized_context,
            );
            metrics.stage(
                &request.model,
                round,
                "scorer",
                scorer_name,
                stage_started.elapsed(),
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
                .filter(|candidate| {
                    candidate.candidate.stage_eligible
                        && (round != RoutingStage::Initial
                            || !request.busy_targets.contains(&(
                                candidate.candidate.route_target_id.clone(),
                                candidate.candidate.data_parallel_rank,
                            )))
                })
                .cloned()
                .collect::<Vec<_>>();
            metrics.candidates(&request.model, round, "selectable", selectable.len());
            if selectable.is_empty() {
                return Err(RouteError::Busy);
            }
            let stage_started = Instant::now();
            let picked =
                pipeline
                    .picker
                    .pick(request, &selectable, routing_progress, customized_context);
            metrics.stage(
                &request.model,
                round,
                "picker",
                picker_name,
                stage_started.elapsed(),
            );
            let picked = picked.ok_or(RouteError::EmptyPickerResult)?;
            let candidate = selectable
                .get(picked.0)
                .map(|candidate| candidate.candidate.clone())
                .ok_or(RouteError::InvalidPickerIndex { index: picked.0 })?;
            // Selection facts and engine load outcomes share the generation request identity.
            // Keep request IDs out of metric labels and never record prompt tokens or cache salts.
            if tracing::enabled!(tracing::Level::DEBUG) {
                let observation = request
                    .kv_prefix_lookup(
                        candidate.route_target_id.as_str(),
                        candidate.data_parallel_rank,
                    )
                    .map_or_else(
                        foretoken_kv_indexer::KvPrefixQueryResult::Unavailable,
                        |lookup| self.kv_prefix_indexer.prefix_matches(lookup),
                    );
                let (cache_matches, unavailable_reason) = match observation {
                    KvPrefixQueryResult::Matches(matches) => (
                        Some(
                            matches
                                .into_iter()
                                .map(|hit| (hit.placement, hit.matched_tokens))
                                .collect::<Vec<_>>(),
                        ),
                        None,
                    ),
                    KvPrefixQueryResult::Unavailable(reason) => (None, Some(reason)),
                };
                tracing::debug!(
                    request_id = %request.request_id(),
                    route_target_id = %candidate.route_target_id.as_str(),
                    data_parallel_rank = candidate.data_parallel_rank,
                    cache_matches = ?cache_matches,
                    cache_unavailable_reason = ?unavailable_reason,
                    "KV routing observation"
                );
            }
            if let Some(on_selected) = on_selected {
                on_selected(&candidate);
            }
            let prefill_tokens = pipeline.scorer.prefill_token_load(
                request,
                &candidate,
                self.kv_prefix_indexer.as_ref(),
            );
            reservations.reserve(request, &candidate, prefill_tokens);
            Ok(candidate)
        })();
        metrics.selection(&request.model, round, started.elapsed(), result.as_ref());
        result
    }

    fn future_stages_available(candidate: &RouteCandidate, candidates: &[RouteCandidate]) -> bool {
        candidate.future_stages().iter().all(|role| {
            candidates.iter().any(|other| {
                other.role == *role && other.pipeline_scope_id == candidate.pipeline_scope_id
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

    /// Selects Aggregate or the initial stage of a complete split pipeline.
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
            |candidate, candidates| match candidate.role {
                ModelServerRole::Aggregate => true,
                ModelServerRole::Prefill => {
                    candidate
                        .pipeline_scope_id
                        .as_ref()
                        .is_some_and(|pipeline_scope_id| {
                            !self.pipeline_scope_has_encoder(request, pipeline_scope_id)
                                && Self::future_stages_available(candidate, candidates)
                        })
                }
                ModelServerRole::Encoder => {
                    candidate.pipeline_scope_id.is_some()
                        && Self::future_stages_available(candidate, candidates)
                }
                ModelServerRole::Decode => false,
            },
            RouteError::NoCompatibleTarget {
                model: request.model.clone(),
            },
        )
    }

    /// Selects Prefill within the Encoder's scope while requiring an eligible Decode stage.
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
            |candidate, candidates| {
                candidate.role == ModelServerRole::Prefill
                    && candidate.pipeline_scope_id.as_deref() == Some(pipeline_scope_id)
                    && Self::future_stages_available(candidate, candidates)
            },
            RouteError::NoCompatibleTarget {
                model: request.model.clone(),
            },
        )
    }

    /// Selects Decode from a fresh observation snapshot within the completed Prefill's scope.
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
