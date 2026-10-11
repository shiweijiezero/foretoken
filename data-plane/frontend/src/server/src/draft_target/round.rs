// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! A request owns its sessions across caller-defined, independently placed rounds.

use std::{collections::BTreeSet, pin::Pin, sync::Arc, time::Instant};

use foretoken_llm_facade::draft_target::{
    DraftTargetRequest, RoleClient, RoleSession, TargetCommit,
};
use foretoken_llm_facade::{LlmFacadeError, LlmFacadeResolver};
use foretoken_model_protocol::{
    DraftTargetCandidateFormat, DraftTargetKvObservation, DraftTargetRoundObservation,
    DraftTargetWorkload,
};
use foretoken_router::{
    DraftTargetPlacement, DraftTargetRoundRoute, RouteDecision, RouteTargetId, Router,
    RouterRequest,
};
use futures::{Stream, StreamExt, stream::FuturesUnordered};
use tokio_util::sync::CancellationToken;

use super::{OutputAdapter, normalize};

/// A schedulable request between rounds; moving it into another batch preserves its sessions.
/// Dropping this owner closes the sessions. A cancellation token interrupts in-flight execution;
/// callers retaining an idle cancelled state must drop it to release its sessions.
pub struct DraftTargetRequestState {
    request: vllm_llm::GenerateRequest,
    pub(super) input: DraftTargetRequest,
    prefix: Vec<u32>,
    remaining: u32,
    version: Option<u64>,
    pub(super) cancellation: CancellationToken,
    output: OutputAdapter,
    target: Option<TargetBinding>,
    draft: Option<DraftBinding>,
    pub(super) closed_workers: BTreeSet<RouteTargetId>,
    ready_at: Instant,
    next_round: u64,
    observation: Option<RoundObservation>,
}

struct TargetBinding {
    client: RoleClient,
    session: RoleSession,
    location: Option<RouteDecision>,
}

struct DraftBinding {
    client: RoleClient,
    session: RoleSession,
    version: u64,
    prefix_len: usize,
}

impl DraftTargetRequestState {
    /// Validates normalized frontend input without opening remote sessions; used at admission.
    pub fn new(
        request: vllm_llm::GenerateRequest,
        cancellation: CancellationToken,
    ) -> Result<Self, LlmFacadeError> {
        let input = normalize(&request)?;
        Ok(Self {
            prefix: input.token_ids.clone(),
            remaining: input.max_tokens,
            input,
            output: OutputAdapter::new(request.clone()),
            request,
            cancellation,
            version: None,
            target: None,
            draft: None,
            closed_workers: BTreeSet::new(),
            ready_at: Instant::now(),
            next_round: 0,
            observation: None,
        })
    }

    /// Returns the original identity for the scheduler's queues and output dispatch.
    pub fn request_id(&self) -> &str {
        &self.request.request_id
    }

    /// Reports the current session/KV location to the scheduler, not a placement requirement.
    /// Fresh requests have no location. The executor derives runnable placement constraints.
    pub fn target_location(&self) -> Option<&RouteDecision> {
        self.target
            .as_ref()
            .and_then(|bound| bound.location.as_ref())
    }

    /// Returns confirmed context length for batching and cost estimation, including the prompt.
    pub fn context_tokens(&self) -> usize {
        self.prefix.len()
    }

    /// Reads current Target KV allocation for an online relocation decision.
    /// A fresh request has no Target; a live session can temporarily have no KV after preemption.
    pub async fn target_kv(&self) -> Result<Option<DraftTargetKvObservation>, LlmFacadeError> {
        match &self.target {
            Some(target) => Ok(Some(target.session.kv().await?)),
            None => Ok(None),
        }
    }

    /// Starts accounting at the executor boundary, shared by inference and relocation rounds.
    fn begin_round(
        &mut self,
        phase: &str,
        draft_endpoint: &str,
        target_endpoint: &str,
        batch_requests: usize,
    ) {
        let now = Instant::now();
        self.observation = Some(RoundObservation {
            started: now,
            proposal_ready: None,
            target_started: if phase != "speculate" {
                Some(now)
            } else {
                None
            },
            value: DraftTargetRoundObservation {
                request_id: self.request_id().to_owned(),
                round: self.next_round,
                version: self.version,
                phase: phase.to_owned(),
                outcome: "aborted".into(),
                draft_endpoint: draft_endpoint.to_owned(),
                target_endpoint: target_endpoint.to_owned(),
                context_tokens: self.prefix.len(),
                batch_requests,
                dispatch_wait_seconds: self.ready_at.elapsed().as_secs_f64(),
                elapsed_seconds: 0.0,
                draft_seconds: None,
                proposal_wait_seconds: None,
                target_seconds: None,
                proposed_tokens: None,
                committed_tokens: None,
                draft: None,
                target: None,
                transfer: None,
                migration: None,
            },
        });
        self.next_round += 1;
    }

    /// Moves a paused request to a caller-selected Target and returns its first resumed output.
    /// This consumes the request; errors, cancellation or dropping the future close both sessions.
    /// Currently requires greedy sampling and compatible full-attention NVIDIA Targets with migration enabled.
    pub async fn migrate_target(
        mut self,
        resolver: &dyn LlmFacadeResolver,
        destination: RouteDecision,
    ) -> Result<DraftTargetRoundOutput, LlmFacadeError> {
        let target = resolver
            .resolve_draft_target(&destination)
            .ok_or(LlmFacadeError::Configuration)?;
        let source = self
            .target
            .as_ref()
            .ok_or(LlmFacadeError::InvalidRequest)?
            .client
            .clone();
        if destination.role != foretoken_model_protocol::ModelServerRole::Target
            || destination.data_parallel_rank != 0
            || source.endpoint() == target.endpoint()
            || self.input.sampling.temperature != 0.0
        {
            return Err(LlmFacadeError::InvalidRequest);
        }
        let draft = self
            .draft
            .as_ref()
            .map(|bound| bound.client.endpoint().to_owned())
            .unwrap_or_default();
        self.begin_round("migrate", &draft, target.endpoint(), 1);
        self.observation
            .as_mut()
            .expect("migration round")
            .value
            .migration = Some(foretoken_model_protocol::DraftTargetMigrationObservation {
            source_target_endpoint: source.endpoint().to_owned(),
            checkpoint_tokens: None,
            export_seconds: None,
        });
        let cancellation = self.cancellation.clone();
        tokio::select! {
            biased;
            () = cancellation.cancelled() => Ok(self.cancelled()),
            result = async {
                let (source_status, target_status) =
                    tokio::try_join!(source.status(), target.status())?;
                if !source_status.kv_migration
                    || !target_status.kv_migration
                    || !target_status.accepting
                    || source_status.model != target_status.model
                    || source_status.revision != target_status.revision
                    || source_status.tokenizer != target_status.tokenizer
                    || source_status.tokenizer_revision != target_status.tokenizer_revision
                    || source_status.candidate_format != target_status.candidate_format
                    || self.prefix.len() + self.remaining as usize > target_status.max_model_len as usize
                {
                    return Err(LlmFacadeError::Configuration);
                }
                let export_started = Instant::now();
                let checkpoint = self.target.as_mut().expect("source Target").session
                    .checkpoint(self.version.expect("paused generation"))
                    .await?;
                if checkpoint.token_ids != self.prefix
                    || checkpoint.computed_tokens >= self.prefix.len()
                {
                    return Err(LlmFacadeError::Protocol);
                }
                let migration = self.observation.as_mut().expect("migration round")
                    .value.migration.as_mut().expect("migration observation");
                migration.checkpoint_tokens = Some(checkpoint.computed_tokens);
                migration.export_seconds = Some(export_started.elapsed().as_secs_f64());
                let mut input = self.input.clone();
                input.token_ids = self.prefix.clone();
                input.max_tokens = self.remaining;
                input.min_tokens = input.min_tokens
                    .saturating_sub(self.input.max_tokens - self.remaining);
                let mut session = target.resume(&input, &source, &checkpoint).await?;
                let commit = session.next_commit().await?;
                // Location changes only after the destination proves it can continue from the KV.
                self.target = Some(TargetBinding {
                    client: target,
                    session,
                    location: Some(destination),
                });
                // Target generation is local to its session; rebind Draft at the new generation.
                self.draft = None;
                Ok(commit)
            } => self.complete(result?),
        }
    }

    /// Receives one Target result, advances the confirmed prefix, and relinquishes terminal state.
    async fn receive(
        mut self,
        target: RoleClient,
        selected_target: Option<RouteDecision>,
    ) -> Result<DraftTargetRoundOutput, LlmFacadeError> {
        let cancellation = self.cancellation.clone();
        if self.target.is_none() {
            let session = tokio::select! {
                biased;
                () = cancellation.cancelled() => return Ok(self.cancelled()),
                result = target.generate(&self.input) => result?,
            };
            let Some(session) = session else {
                let worker = selected_target
                    .ok_or(LlmFacadeError::Unavailable)?
                    .route_target_id;
                return Ok(self.requeue(worker));
            };
            // Publish location only after the worker owns a live session.
            self.target = Some(TargetBinding {
                client: target,
                session,
                location: selected_target,
            });
        }
        let commit = tokio::select! {
            biased;
            () = cancellation.cancelled() => return Ok(self.cancelled()),
            result = self.target.as_mut().expect("opened Target").session.next_commit() => result?,
        };
        self.complete(commit)
    }

    /// Validates the Target delta and returns the next schedulable state or terminal output.
    fn complete(mut self, commit: TargetCommit) -> Result<DraftTargetRoundOutput, LlmFacadeError> {
        self.remaining = self
            .remaining
            .checked_sub(
                u32::try_from(commit.token_ids.len()).map_err(|_| LlmFacadeError::Protocol)?,
            )
            .ok_or(LlmFacadeError::Protocol)?;
        if commit.finished {
            if commit.version.is_some() || commit.finish_reason.is_none() {
                return Err(LlmFacadeError::Protocol);
            }
        } else if commit.version.is_none() || self.remaining == 0 {
            return Err(LlmFacadeError::Protocol);
        }
        self.prefix.extend_from_slice(&commit.token_ids);
        self.version = commit.version;
        let finished = commit.finished;
        let mut observation = self.observation.take().expect("executing round");
        observation.value.target_seconds = Some(
            observation
                .target_started
                .expect("Target started")
                .elapsed()
                .as_secs_f64(),
        );
        observation.value.committed_tokens = Some(commit.token_ids.len());
        observation.value.target = Some(commit.observation.clone());
        observation.value.transfer = commit.transfer.clone();
        let output = self.output.convert(commit)?;
        let observation = observation.finish("completed");
        self.ready_at = Instant::now();
        Ok(DraftTargetRoundOutput {
            request_id: self.request_id().to_owned(),
            output: Some(output),
            observation,
            continuation: if finished { None } else { Some(self) },
        })
    }

    fn cancelled(mut self) -> DraftTargetRoundOutput {
        let observation = self
            .observation
            .take()
            .expect("executing round")
            .finish("cancelled");
        DraftTargetRoundOutput {
            request_id: self.request_id().to_owned(),
            output: None,
            observation,
            continuation: None,
        }
    }

    /// Returns an unexecuted member for new placement without advancing its Target or prefix.
    fn requeue(mut self, worker: RouteTargetId) -> DraftTargetRoundOutput {
        self.closed_workers.insert(worker);
        let observation = self
            .observation
            .take()
            .expect("executing round")
            .finish("admission_closed");
        self.ready_at = Instant::now();
        DraftTargetRoundOutput {
            request_id: self.request_id().to_owned(),
            output: None,
            observation,
            continuation: Some(self),
        }
    }

    /// Reuses the selected Draft's binding, or binds the full confirmed prefix on a new Draft.
    /// None means admission was explicitly refused before any proposal work started.
    async fn propose(
        &mut self,
        draft: &RoleClient,
        budget: u32,
    ) -> Result<Option<foretoken_llm_facade::draft_target::DraftProposal>, LlmFacadeError> {
        let version = self.version.expect("confirmed request");
        if self
            .draft
            .as_ref()
            .is_some_and(|bound| bound.client.endpoint() != draft.endpoint())
        {
            self.draft.take();
        }
        if let Some(bound) = &mut self.draft {
            bound
                .session
                .commit(bound.version, version, &self.prefix[bound.prefix_len..])
                .await?;
            bound.version = version;
            bound.prefix_len = self.prefix.len();
        } else {
            let Some(session) = draft
                .bind(&self.prefix, version, &self.input.sampling)
                .await?
            else {
                return Ok(None);
            };
            self.draft = Some(DraftBinding {
                session,
                client: draft.clone(),
                version,
                prefix_len: self.prefix.len(),
            });
        }
        self.draft
            .as_mut()
            .expect("bound Draft")
            .session
            .propose(
                version,
                budget.min(self.remaining),
                self.target
                    .as_ref()
                    .expect("bound Target")
                    .session
                    .context_source(),
            )
            .await
            .map(Some)
    }
}

/// One member's confirmed output and ownership returned to the caller for the next round.
/// Finished/cancelled members have no continuation. A refused session returns a continuation
/// without output so the caller can select placement again; cancellation has neither.
pub struct DraftTargetRoundOutput {
    pub request_id: String,
    pub observation: DraftTargetRoundObservation,
    pub output: Option<vllm_llm::GenerateOutput>,
    pub continuation: Option<DraftTargetRequestState>,
}

/// Returns members independently so pending prefill cannot block ready requests.
pub type DraftTargetRoundStream =
    Pin<Box<dyn Stream<Item = Result<DraftTargetRoundOutput, LlmFacadeError>> + Send>>;

/// A selected round owns exactly its members and routing reservations until execution or drop.
/// Construction does not run inference; `execute` performs at most one step per member.
pub struct DraftTargetRound {
    draft: RoleClient,
    target: RoleClient,
    members: Vec<DraftTargetRequestState>,
    budget: u32,
    pub(super) route: Option<DraftTargetRoundRoute>,
    workloads: (DraftTargetWorkload, DraftTargetWorkload),
}

/// Selects workers for the caller's current batch, respecting existing Target KV ownership.
/// Uses current prefixes and remaining budgets, with constraints supplied by the executor.
/// A selected round takes the members from the caller. `None` leaves them untouched when no
/// compatible worker is currently selectable; the caller waits for routing state to change.
/// Invalid requests and routing failures are errors, not queueable capacity signals.
pub async fn select_draft_target_round(
    router: &dyn Router,
    resolver: &dyn LlmFacadeResolver,
    model: &str,
    members: &mut Vec<DraftTargetRequestState>,
) -> Result<Option<DraftTargetRound>, crate::GenerationError> {
    let placement = DraftTargetPlacement {
        required_target: DraftTargetRound::required_target(members)?,
        closed_workers: members
            .iter()
            .flat_map(|member| member.closed_workers.iter().cloned())
            .collect(),
    };
    let requests = members
        .iter()
        .map(|member| {
            let mut request = member.request.clone();
            request.prompt_token_ids = member.prefix.clone();
            request.sampling_params.max_tokens = member.remaining;
            RouterRequest::new(model, Arc::new(request))
        })
        .collect::<Vec<_>>();
    let route = match router.route_draft_target_round(&requests, &placement).await {
        Ok(route) => route,
        Err(foretoken_router::RouteError::NoMatchingRouteTarget { .. }) => return Ok(None),
        Err(foretoken_router::RouteError::InvalidBatch) => {
            return Err(crate::GenerationError::InvalidRequest);
        }
        Err(_) => return Err(crate::GenerationError::Internal),
    };
    let (draft, target) = route.decisions();
    let draft = resolver
        .resolve_draft_target(draft)
        .ok_or(crate::GenerationError::Internal)?;
    let target = resolver
        .resolve_draft_target(target)
        .ok_or(crate::GenerationError::Internal)?;
    Ok(Some(
        DraftTargetRound::prepare(draft, target, std::mem::take(members), Some(route)).await?,
    ))
}

impl DraftTargetRound {
    /// Derives the current executor's Target constraint for scheduler grouping and Router selection.
    /// Inference rounds reuse the current worker; call `migrate_target` before regrouping
    /// requests from different locations. An all-fresh batch has no Target requirement.
    pub fn required_target(
        members: &[DraftTargetRequestState],
    ) -> Result<Option<RouteDecision>, crate::GenerationError> {
        let location = members.iter().find_map(|member| member.target_location());
        if members
            .iter()
            .filter_map(|member| member.target_location())
            .any(|other| Some(other) != location)
        {
            return Err(crate::GenerationError::InvalidRequest);
        }
        Ok(location.cloned())
    }

    /// Checks selected worker capabilities before admitting a round or rebinding any request.
    pub(super) async fn prepare(
        draft: RoleClient,
        target: RoleClient,
        members: Vec<DraftTargetRequestState>,
        route: Option<DraftTargetRoundRoute>,
    ) -> Result<Self, LlmFacadeError> {
        let (d, t) = tokio::try_join!(draft.status(), target.status())?;
        if d.role != "draft"
            || t.role != "target"
            || d.candidate_format != t.candidate_format
            || d.context_profile != t.context_profile
        {
            return Err(LlmFacadeError::Configuration);
        }
        let budget = d.token_budget.min(t.token_budget);
        if budget == 0 {
            return Err(LlmFacadeError::Configuration);
        }
        let limit = d.max_model_len.min(t.max_model_len) as usize;
        for member in &members {
            if member
                .target
                .as_ref()
                .is_some_and(|bound| bound.client.endpoint() != target.endpoint())
            {
                // Moving Target is an explicit migration operation before this inference round.
                return Err(LlmFacadeError::Configuration);
            }
            if member.prefix.is_empty()
                || member.remaining == 0
                || member
                    .prefix
                    .len()
                    .saturating_add(member.remaining as usize)
                    > limit
                || (member.input.sampling.temperature != 0.0
                    && d.candidate_format != DraftTargetCandidateFormat::TokenIdsLogProbs)
            {
                return Err(LlmFacadeError::InvalidRequest);
            }
        }
        Ok(Self {
            draft,
            target,
            members,
            budget,
            route,
            workloads: (
                d.workload.ok_or(LlmFacadeError::Protocol)?,
                t.workload.ok_or(LlmFacadeError::Protocol)?,
            ),
        })
    }

    /// Reports the two live worker snapshots read while preparing this round, in Draft/Target order.
    /// Collection timestamps let a scheduler distinguish observations from current reservations.
    pub fn workloads(&self) -> (&DraftTargetWorkload, &DraftTargetWorkload) {
        (&self.workloads.0, &self.workloads.1)
    }

    /// Executes one prefill or proposal/verification step and returns each member as it completes.
    /// Dropping the stream closes members still owned by this round. Returned continuations belong
    /// to the caller; they can be regrouped immediately while other members are still executing.
    pub fn execute(self) -> DraftTargetRoundStream {
        let Self {
            draft,
            target,
            mut members,
            budget,
            mut route,
            workloads: _,
        } = self;
        let count = members.len();
        for member in &mut members {
            let phase = if member.target.is_none() {
                "prefill"
            } else {
                "speculate"
            };
            member.begin_round(phase, draft.endpoint(), target.endpoint(), count);
        }
        let (fresh, ready): (Vec<_>, Vec<_>) = members
            .into_iter()
            .partition(|member| member.target.is_none());
        let selected_target = route.as_ref().map(|route| route.decisions().1.clone());
        let selected_draft = route
            .as_ref()
            .map(|route| route.decisions().0.route_target_id.clone());
        let prefill = fresh
            .into_iter()
            .map(|member| member.receive(target.clone(), selected_target.clone()))
            .collect::<FuturesUnordered<_>>();
        let verification = verify(draft, target, ready, budget, selected_draft);
        Box::pin(async_stream::try_stream! {
            let mut results = futures::stream::select(prefill, verification);
            while let Some(result) = results.next().await {
                let event = result?;
                if let Some(route) = &mut route {
                    route.finish(&event.request_id);
                }
                yield event;
            }
        })
    }
}

/// Proposes concurrently, admits one verification batch, then returns confirmed members independently.
fn verify(
    draft: RoleClient,
    target: RoleClient,
    members: Vec<DraftTargetRequestState>,
    budget: u32,
    selected_draft: Option<RouteTargetId>,
) -> DraftTargetRoundStream {
    Box::pin(async_stream::try_stream! {
        let mut proposals = FuturesUnordered::new();
        for mut member in members {
            let draft = draft.clone();
            proposals.push(async move {
                let cancellation = member.cancellation.clone();
                let result = tokio::select! {
                    biased;
                    () = cancellation.cancelled() => None,
                    result = member.propose(&draft, budget) => Some(result),
                };
                (member, result)
            });
        }
        let mut ready: Vec<Option<DraftTargetRequestState>> = Vec::new();
        let mut entries = Vec::new();
        // A ready proposal still owns both sessions while sibling Draft work is pending.
        // Keep its cancellation active through collection and Target tensor admission.
        let mut cancellations = FuturesUnordered::new();
        while !proposals.is_empty() {
            let (mut member, result) = tokio::select! {
                biased;
                Some(position) = cancellations.next(), if !cancellations.is_empty() => {
                    let position: usize = position;
                    if let Some(member) = ready[position].take() { yield member.cancelled(); }
                    continue;
                }
                Some(result) = proposals.next() => result,
            };
            let proposal = match result.transpose()? {
                None => {
                    yield member.cancelled();
                    continue;
                }
                Some(None) => {
                    // Only an explicit pre-binding refusal is safe to place again. A direct
                    // fixed-pair caller has no alternative inventory and reports unavailable.
                    let draft_id = selected_draft.clone().ok_or(LlmFacadeError::Unavailable)?;
                    yield member.requeue(draft_id);
                    continue;
                }
                Some(Some(proposal)) => proposal,
            };
            let observation = member.observation.as_mut().expect("executing round");
            observation.value.draft_seconds = Some(observation.started.elapsed().as_secs_f64());
            observation.value.proposed_tokens = Some(proposal.token_ids.len());
            observation.value.draft = Some(proposal.observation.clone());
            observation.proposal_ready = Some(Instant::now());
            entries.push(
                member
                    .target
                    .as_ref()
                    .expect("live Target")
                    .session
                    .verification(proposal, &draft),
            );
            let position = ready.len();
            let cancellation = member.cancellation.clone();
            cancellations.push(async move {
                cancellation.cancelled().await;
                position
            });
            ready.push(Some(member));
        }
        if ready.iter().all(Option::is_none) {
            return;
        }
        for member in ready.iter_mut().flatten() {
            let observation = member.observation.as_mut().expect("executing round");
            observation.value.proposal_wait_seconds = Some(observation.proposal_ready.expect("proposal ready").elapsed().as_secs_f64());
            observation.target_started = Some(Instant::now());
        }
        let admission = target.verify_batch(&entries);
        tokio::pin!(admission);
        let accepted = loop {
            tokio::select! {
                biased;
                Some(position) = cancellations.next(), if !cancellations.is_empty() => {
                    if let Some(member) = ready[position].take() { yield member.cancelled(); }
                }
                result = &mut admission => break result,
            }
        };
        let accepted = accepted?;
        let mut commits = FuturesUnordered::new();
        for (member, accepted) in ready.into_iter().zip(accepted) {
            if let Some(member) = member {
                if member.cancellation.is_cancelled() {
                    yield member.cancelled();
                } else {
                    if !accepted {
                        Err(LlmFacadeError::Rejected)?;
                    }
                    commits.push(member.receive(target.clone(), None));
                }
            }
        }
        while let Some(result) = commits.next().await {
            yield result?;
        }
    })
}

/// Owns a round's accounting even when its execution stream is dropped or fails.
struct RoundObservation {
    started: Instant,
    proposal_ready: Option<Instant>,
    target_started: Option<Instant>,
    value: DraftTargetRoundObservation,
}
impl RoundObservation {
    fn finish(mut self, outcome: &str) -> DraftTargetRoundObservation {
        self.value.outcome = outcome.to_owned();
        self.value.elapsed_seconds = self.started.elapsed().as_secs_f64();
        self.value.clone()
    }
}
impl Drop for RoundObservation {
    fn drop(&mut self) {
        self.value.elapsed_seconds = self.started.elapsed().as_secs_f64();
        foretoken_metrics::record_draft_target_round(&self.value);
    }
}
