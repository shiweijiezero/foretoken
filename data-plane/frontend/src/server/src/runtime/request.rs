// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Executes generation and CPU-only requests through the published runtime and shared admission.

use std::sync::Arc;
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use async_trait::async_trait;
use foretoken_admission::{AdmissionAttempt, AdmissionError, AdmissionPermit, AdmissionRequest};
use foretoken_chat::{ChatRequest, NewChatOutputProcessorOptions};
use foretoken_router::{RouteTargetSet, RouterRequest};
use foretoken_text::{Prompt, TextRequest};

use super::generation::RuntimeSlot;
use super::timing::{
    before_deadline, before_waiting_deadline, deadline_stream, prepare_with_permit,
};
use super::workflow::execute_workflow;
use super::{
    Generated, GeneratedChat, Generation, GenerationError, GenerationRequest, KvIndexDiagnostics,
    ModelRuntime, PreparationTargets, RequestSnapshot, RoutedGenerate, RoutedRequest,
    RuntimeDiagnostics, RuntimeGeneration, Tokenization,
};
use crate::{AdmissionOrigin, admission};

/// Observes a request independently of capacity ownership, including detached CPU preparation.
struct AdmissionObservation(Option<AdmissionAttempt>);

#[async_trait]
impl Generation for RuntimeGeneration {
    fn request_snapshot(&self) -> Result<RequestSnapshot, GenerationError> {
        let slot = self.ready_state()?;
        let snapshot = RequestSnapshot {
            models: slot.models.clone(),
            request_timeout: slot.state.request_timeout,
            stream_idle: slot.state.stream_idle,
            generation: Arc::new(Self {
                request_slot: Some(slot),
                ..self.clone()
            }),
        };
        Ok(snapshot)
    }

    async fn admit(&self, request: &AdmissionRequest) -> Result<AdmissionPermit, GenerationError> {
        self.acquire_admission(request).await
    }

    fn video_task_service(
        &self,
        model: &str,
    ) -> Result<super::VideoServiceReference, GenerationError> {
        let slot = self.ready_state()?;
        if !slot.models.iter().any(|candidate| candidate == model) {
            return Err(GenerationError::ModelNotFound);
        }
        if slot.state.models.contains_key(model) {
            return Err(GenerationError::InvalidRequest);
        }
        let video = slot
            .state
            .video
            .as_ref()
            .ok_or(GenerationError::Unavailable)?;
        video.service(model).cloned()
    }

    async fn generate_video(
        &self,
        request: crate::VideoRequest,
    ) -> Result<axum::response::Response, GenerationError> {
        let slot = self.generation_slot(&request.model, None).await?;
        let video = slot
            .state
            .video
            .as_ref()
            .ok_or(GenerationError::Unavailable)?;
        if !video.models.contains_key(&request.model) {
            return Err(GenerationError::InvalidRequest);
        }
        // Resolve and dispatch within this one generation; a task must not migrate to another service.
        if let Some(expected) = &request.model_service_uid
            && &video.service(&request.model)?.uid != expected
        {
            return Err(GenerationError::Unavailable);
        }
        let context = RouterRequest::video(request.model.clone(), request.request_id.clone());
        let mut session = slot.state.router.start(context).await;
        let decision = session
            .select_initial()
            .map_err(|_| GenerationError::Unavailable)?;
        if request.model_service_uid.as_ref().is_some_and(|expected| {
            !video.inventory.model_routes().routes().iter().any(|route| {
                route.route_target_id == decision.route_target_id
                    && &route.target.service_uid == expected
            })
        }) {
            return Err(GenerationError::Unavailable);
        }
        let endpoint = video
            .inventory
            .http_endpoint(&decision)
            .ok_or(GenerationError::Unavailable)?;
        let _queue = foretoken_metrics::QueueGuard::backend_dispatch(&decision.admission_targets);
        crate::video::forward(
            &video.client,
            &endpoint,
            request,
            session,
            self.request_timeout()?,
        )
        .await
    }

    async fn generate(&self, mut request: GenerationRequest) -> Result<Generated, GenerationError> {
        let deadline = tokio::time::Instant::from_std(request.started_at + self.request_timeout()?);
        let mut observation = AdmissionObservation(
            request
                .admission
                .as_ref()
                .and_then(AdmissionPermit::observation),
        );
        let result = before_deadline(deadline, async {
            let admission = match request.admission.take() {
                Some(permit) => permit,
                None => self.admit(&admission::generation(&request)).await?,
            };
            observation.0 = admission.observation();
            let slot = self
                .generation_slot(&request.model, Some(&admission))
                .await?;
            let runtime = slot.state.model(&request.model)?;
            request.cache_salt = runtime.resolve_cache_salt(request.cache_salt.take())?;
            let text_request = TextRequest {
                request_id: request.request_id.clone(),
                prompt: request.prompt.clone(),
                mm_features: None,
                sampling_params: request.sampling_params.clone(),
                decode_options: request.decode_options.clone(),
                intermediate: request.intermediate,
                priority: request.priority,
                cache_salt: request.cache_salt.clone(),
                add_special_tokens: false,
                data_parallel_rank: None,
                session_id: request.session_id.clone(),
                reasoning_parser_kwargs: None,
                lora_request: None,
                arrival_time: request.arrival_time,
            };
            self.dispatch(runtime, request, text_request, admission)
                .await
        })
        .await;
        if let Err(error) = &result {
            observation.failed(*error);
        }
        let mut generated = result?;
        generated.routed.stream = deadline_stream(generated.routed.stream, deadline);
        Ok(generated)
    }

    async fn generate_chat(
        &self,
        mut request: GenerationRequest,
        mut chat: ChatRequest,
        include_reasoning: bool,
    ) -> Result<GeneratedChat, GenerationError> {
        let deadline = tokio::time::Instant::from_std(request.started_at + self.request_timeout()?);
        let mut observation = AdmissionObservation(
            request
                .admission
                .as_ref()
                .and_then(AdmissionPermit::observation),
        );
        let result = before_deadline(deadline, async {
            let admission = match request.admission.take() {
                Some(permit) => permit,
                None => self.admit(&admission::generation(&request)).await?,
            };
            observation.0 = admission.observation();
            let slot = self
                .generation_slot(&request.model, Some(&admission))
                .await?;
            let runtime = slot.state.model(&request.model)?;
            chat.cache_salt = runtime.resolve_cache_salt(chat.cache_salt.take())?;
            let bundle = runtime.bundle.clone();
            let tool_parser = request.tool_call_parser.clone();
            let reasoning_parser = request.reasoning_parser.clone();
            let ((mut text_request, output_processor), admission) =
                prepare_with_permit(admission, async move {
                    bundle
                        .chat_processor
                        .prepare_with_options(
                            chat,
                            NewChatOutputProcessorOptions {
                                tool_call_parser: &tool_parser,
                                reasoning_parser: &reasoning_parser,
                            },
                        )
                        .await
                        .map_err(|error| {
                            if error.is_request_validation_error() {
                                GenerationError::InvalidRequest
                            } else {
                                GenerationError::Internal
                            }
                        })
                })
                .await?;
            // The chat renderer stamps its own entry time after runtime admission. Preserve the
            // HTTP handler's earlier origin so chat and text requests include the same wait.
            text_request.arrival_time = request.arrival_time.or(text_request.arrival_time);
            let generated = self
                .dispatch(runtime, request, text_request, admission)
                .await?;
            Ok(GeneratedChat {
                generated,
                output_processor,
                include_reasoning,
            })
        })
        .await;
        if let Err(error) = &result {
            observation.failed(*error);
        }
        let mut chat = result?;
        chat.generated.routed.stream = deadline_stream(chat.generated.routed.stream, deadline);
        Ok(chat)
    }

    async fn tokenize(
        &self,
        model: &str,
        prompt: Prompt,
        add_special_tokens: bool,
        return_token_strs: bool,
        origin: AdmissionOrigin,
    ) -> Result<Tokenization, GenerationError> {
        let deadline = tokio::time::Instant::from_std(origin.received_at + self.request_timeout()?);
        let mut observation = AdmissionObservation(None);
        let result = before_deadline(deadline, async {
            let admission = self
                .acquire_admission(&admission::preprocessing(model, origin))
                .await?;
            observation.0 = admission.observation();
            before_waiting_deadline(admission.waiting_deadline(), async {
                let slot = self.generation_slot(model, Some(&admission)).await?;
                let runtime = slot.state.model(model)?;
                let tokenizer = &runtime.bundle.tokenizer;
                let token_ids = match prompt {
                    Prompt::Text(text) => tokenizer
                        .encode(&text, add_special_tokens)
                        .map_err(|_| GenerationError::InvalidRequest)?,
                    Prompt::TokenIds(token_ids) => token_ids,
                };
                let token_strs = if return_token_strs {
                    Some(
                        token_ids
                            .iter()
                            .map(|token_id| tokenizer.id_to_token(*token_id))
                            .collect::<Option<Vec<_>>>()
                            .ok_or(GenerationError::InvalidRequest)?,
                    )
                } else {
                    None
                };
                Ok(Tokenization {
                    token_ids,
                    token_strs,
                    max_model_len: runtime.bundle.text_processor.max_model_len(),
                })
            })
            .await
        })
        .await;
        observation.prepared(&result);
        result
    }

    async fn tokenize_chat(
        &self,
        model: &str,
        chat: ChatRequest,
        return_token_strs: bool,
        origin: AdmissionOrigin,
    ) -> Result<Tokenization, GenerationError> {
        let deadline = tokio::time::Instant::from_std(origin.received_at + self.request_timeout()?);
        let mut observation = AdmissionObservation(None);
        let result = before_deadline(deadline, async {
            let admission = self
                .acquire_admission(&admission::preprocessing(model, origin))
                .await?;
            observation.0 = admission.observation();
            let slot = self.generation_slot(model, Some(&admission)).await?;
            let bundle = slot.state.model(model)?.bundle.clone();
            let (tokens, _admission) = prepare_with_permit(admission, async move {
                let text_request = bundle
                    .chat_processor
                    .prepare_for_tokenization(chat)
                    .await
                    .map_err(|_| GenerationError::InvalidRequest)?;
                let prepared = bundle
                    .text_processor
                    .prepare(text_request)
                    .map_err(|_| GenerationError::InvalidRequest)?;
                let token_ids = prepared.generate_request.prompt_token_ids;
                let tokenizer = &bundle.tokenizer;
                let token_strs = if return_token_strs {
                    Some(
                        token_ids
                            .iter()
                            .map(|token_id| tokenizer.id_to_token(*token_id))
                            .collect::<Option<Vec<_>>>()
                            .ok_or(GenerationError::InvalidRequest)?,
                    )
                } else {
                    None
                };
                Ok(Tokenization {
                    token_ids,
                    token_strs,
                    max_model_len: bundle.text_processor.max_model_len(),
                })
            })
            .await?;
            Ok(tokens)
        })
        .await;
        observation.prepared(&result);
        result
    }

    async fn detokenize(
        &self,
        model: &str,
        token_ids: &[u32],
        origin: AdmissionOrigin,
    ) -> Result<String, GenerationError> {
        let deadline = tokio::time::Instant::from_std(origin.received_at + self.request_timeout()?);
        let mut observation = AdmissionObservation(None);
        let result = before_deadline(deadline, async {
            let admission = self
                .acquire_admission(&admission::preprocessing(model, origin))
                .await?;
            observation.0 = admission.observation();
            before_waiting_deadline(admission.waiting_deadline(), async {
                let slot = self.generation_slot(model, Some(&admission)).await?;
                let runtime = slot.state.model(model)?;
                runtime
                    .bundle
                    .tokenizer
                    .decode(token_ids, false)
                    .map_err(|_| GenerationError::InvalidRequest)
            })
            .await
        })
        .await;
        observation.prepared(&result);
        result
    }

    fn ready(&self) -> bool {
        self.accepting.load(Ordering::Acquire) && self.slot.load().is_some()
    }

    async fn refresh_admission_observations(&self) {
        self.admission.refresh_observations().await;
    }

    fn diagnostics(&self) -> RuntimeDiagnostics {
        let status = self
            .configuration
            .lock()
            .expect("configuration status lock poisoned")
            .clone();
        let Some(slot) = self.slot.load_full() else {
            return RuntimeDiagnostics {
                serving_ready: false,
                active_generation: None,
                target_generation: status.target_generation,
                configuration_error: status.error,
                kv_index: KvIndexDiagnostics {
                    state: "unavailable".into(),
                    reason: Some("no_active_generation".into()),
                    sources_healthy: 0,
                    sources_total: 0,
                },
            };
        };
        RuntimeDiagnostics {
            serving_ready: self.accepting.load(Ordering::Acquire) && slot.control.is_ready(),
            active_generation: self.admission.is_applied().then_some(slot.version),
            target_generation: status.target_generation,
            configuration_error: status.error,
            kv_index: slot.control.kv_index_diagnostics(),
        }
    }
}

impl RuntimeGeneration {
    /// Reserves model waiting capacity under the original HTTP deadline before preparation.
    async fn acquire_admission(
        &self,
        request: &AdmissionRequest,
    ) -> Result<AdmissionPermit, GenerationError> {
        let deadline =
            tokio::time::Instant::from_std(request.received_at + self.request_timeout()?);
        let admission = self.model_admission(&request.model)?;
        admission
            .enter(request, deadline)
            .await
            .map_err(GenerationError::from)
    }

    /// Waits for a healthy runtime slot used by generation and CPU-only preparation.
    async fn generation_slot(
        &self,
        model: &str,
        admission: Option<&AdmissionPermit>,
    ) -> Result<Arc<RuntimeSlot>, GenerationError> {
        // Admission retains waiting ownership while readiness changes; runtime publication
        // supplies observations and wakeups, not an independent preparation queue.
        let waiting_deadline = admission.and_then(AdmissionPermit::waiting_deadline);
        let mut publication_updates = self.publication_updates.subscribe();
        let mut _queued = None;
        let mut observed_version = None;
        loop {
            let slot = self.ready_state()?;
            let allowed_pools = match admission {
                Some(permit) => permit.waiting_rules().await?.allowed_pools,
                None => Vec::new(),
            };
            if (slot.state.models.contains_key(model)
                || slot
                    .state
                    .video
                    .as_ref()
                    .is_some_and(|video| video.models.contains_key(model)))
                && slot.control.model_ready(model, &allowed_pools)
            {
                return Ok(slot);
            }
            if !slot
                .control
                .configured_models()
                .iter()
                .any(|configured| configured == model)
            {
                return Err(GenerationError::ModelNotFound);
            }
            if observed_version != Some(slot.version) {
                let targets = slot
                    .state
                    .preparation_targets
                    .get(model)
                    .and_then(|targets| targets.select(&allowed_pools))
                    .ok_or(GenerationError::Unavailable)?;
                _queued = Some(foretoken_metrics::QueueGuard::runtime_preparation(&targets));
                observed_version = Some(slot.version);
            }
            let changed = if let Some(deadline) = waiting_deadline {
                tokio::time::timeout_at(deadline, publication_updates.changed())
                    .await
                    .map_err(|_| GenerationError::QueueTimeout)?
            } else {
                publication_updates.changed().await
            };
            if changed.is_err() || !self.accepting.load(Ordering::Acquire) {
                return Err(GenerationError::Unavailable);
            }
            // Reload current routing after readiness or publication changes, retaining the
            // original request budget and its shared admission reservation.
        }
    }

    /// Prepares and dispatches one request, transferring admission and route cleanup to its stream.
    async fn dispatch(
        &self,
        runtime: &ModelRuntime,
        request: GenerationRequest,
        text_request: TextRequest,
        admission: AdmissionPermit,
    ) -> Result<Generated, GenerationError> {
        let bundle = &runtime.bundle;
        let decode_options = text_request.decode_options.clone();
        let prepared = bundle
            .text_processor
            .prepare(text_request)
            .map_err(|error| {
                if error.is_request_validation_error() {
                    GenerationError::InvalidRequest
                } else {
                    GenerationError::Internal
                }
            })?;
        // Tokenization is synchronous; do not admit backend work if it exhausted the budget.
        if Instant::now() >= request.started_at + self.request_timeout()? {
            return Err(GenerationError::DeadlineExceeded);
        }
        let generate_request = prepared.generate_request;
        let mut busy_targets = Vec::new();
        // Preparation holds only waiting capacity. Dispatch grants cover the whole batch;
        // explicit initial nonacceptance returns that grant without resetting queue position.
        // Ambiguous submissions and failures after a stage was accepted never enter this loop.
        let (decision, backend_stream, mut session) = loop {
            // Readiness waiting cannot consume a dispatch reservation. After the caller's
            // turn, reload routing rather than binding prepared input to an earlier snapshot.
            self.generation_slot(&request.model, Some(&admission))
                .await?;
            let rules = before_waiting_deadline(admission.waiting_deadline(), async {
                admission.dispatch().await.map_err(GenerationError::from)
            })
            .await?;
            let active = self.ready_state()?;
            if !Arc::ptr_eq(&runtime.bundle, &active.state.model(&request.model)?.bundle) {
                return Err(GenerationError::Unavailable);
            }
            let mut context =
                RouterRequest::new(request.model.clone(), Arc::new(generate_request.clone()));
            context.allowed_pools = rules.allowed_pools;
            context.busy_targets = busy_targets.clone();
            let mut session = before_waiting_deadline(admission.waiting_deadline(), async {
                Ok(active.state.router.start(context).await)
            })
            .await?;
            let result = match session.select_initial() {
                Ok(initial) => {
                    let selected = (initial.route_target_id.clone(), initial.data_parallel_rank);
                    let _queue =
                        foretoken_metrics::QueueGuard::backend_dispatch(&initial.admission_targets);
                    let result = execute_workflow(
                        &*active.state.resolver,
                        &mut *session,
                        initial,
                        generate_request.clone(),
                        &admission,
                        rules.priority,
                    )
                    .await;
                    if matches!(result, Err(GenerationError::BackendBusy)) {
                        busy_targets.push(selected);
                    }
                    result
                }
                Err(foretoken_router::RouteError::Busy) => {
                    // Every currently eligible target has been tried or policy-filtered. A
                    // later admission turn starts a fresh round with fresh route observations.
                    busy_targets.clear();
                    Err(GenerationError::BackendBusy)
                }
                Err(error) => return Err(error.into()),
            };
            match result {
                Ok((decision, stream)) => break (decision, stream, session),
                Err(GenerationError::BackendBusy) => {
                    drop(session);
                    if !admission.retry().await? {
                        if admission.is_reserved() {
                            return Err(GenerationError::Overloaded);
                        }
                        before_waiting_deadline(admission.waiting_deadline(), async {
                            tokio::time::sleep(Duration::from_millis(100)).await;
                            Ok(())
                        })
                        .await?;
                    }
                }
                Err(error) => return Err(error),
            }
        };
        // The response stream owns the work permit and routing load through completion or cancellation.
        let stream = Box::pin(async_stream::stream! {
            use futures::StreamExt;
            let _admission = admission;
            let mut backend_stream = backend_stream;
            let mut response_started = false;
            while let Some(output) = backend_stream.next().await {
                if !response_started {
                    session.response_started();
                    response_started = true;
                }
                if let Ok(output) = &output {
                    session.output_tokens_generated(output.token_ids.len());
                }
                let terminal = output.as_ref().map_or(true, |output| output.finish_reason.is_some());
                if terminal { session.stage_complete(); }
                yield output;
                if terminal { break; }
            }
            session.stage_complete();
        });
        let routed = RoutedGenerate {
            routed_request: RoutedRequest {
                decision,
                request: generate_request,
            },
            stream,
        };
        Ok(Generated {
            routed,
            tokenizer: bundle.tokenizer.clone(),
            decode_options,
        })
    }
}

impl ModelRuntime {
    /// Resolves the model-owned cache salt before preprocessing using the captured caller identity.
    fn resolve_cache_salt(
        &self,
        requested: Option<String>,
    ) -> Result<Option<String>, GenerationError> {
        match self.prefix_cache_isolation {
            Some(true) => foretoken_admission::CallerIdentity::current()
                .caller
                .filter(|caller| !caller.is_empty())
                .map(Some)
                .ok_or(GenerationError::InvalidRequest),
            Some(false) => Ok(None),
            None => Ok(requested),
        }
    }
}

impl PreparationTargets {
    /// Attributes readiness demand to allowed Pools; the controller validates required stages.
    fn select(&self, allowed_pools: &[String]) -> Option<RouteTargetSet> {
        let candidates = self
            .candidates
            .iter()
            .map(|candidate| {
                RouteTargetSet::new(
                    candidate
                        .targets()
                        .iter()
                        .filter(|target| {
                            allowed_pools.is_empty() || allowed_pools.contains(&target.name)
                        })
                        .cloned()
                        .collect(),
                )
            })
            .filter(|candidate| !candidate.targets().is_empty())
            .collect::<Vec<_>>();
        if candidates.is_empty() {
            return None;
        }
        let index = self.next.fetch_add(1, Ordering::Relaxed) as usize % candidates.len();
        Some(candidates[index].clone())
    }
}

impl AdmissionObservation {
    /// Completes CPU-only work without reporting a backend dispatch opportunity.
    fn prepared<T>(&self, result: &Result<T, GenerationError>) {
        match result {
            Ok(_) => {
                if let Some(attempt) = &self.0 {
                    attempt.prepared();
                }
            }
            Err(error) => self.failed(*error),
        }
    }

    /// Records a terminal frontend error; explicit Busy retries remain inside dispatch.
    fn failed(&self, error: GenerationError) {
        let Some(attempt) = &self.0 else {
            return;
        };
        let error = match error {
            GenerationError::QueueTimeout => AdmissionError::QueueTimeout,
            GenerationError::DeadlineExceeded => AdmissionError::DeadlineExceeded,
            GenerationError::InvalidRequest => {
                attempt.invalid_request();
                return;
            }
            GenerationError::Overloaded => AdmissionError::Overloaded,
            GenerationError::AdmissionCapacityExceeded => AdmissionError::BatchTooLarge,
            _ => {
                attempt.dispatch_failed();
                return;
            }
        };
        attempt.complete(&Err::<(), _>(error));
    }
}

impl Drop for AdmissionObservation {
    fn drop(&mut self) {
        if let Some(attempt) = &self.0 {
            attempt.cancelled();
        }
    }
}
