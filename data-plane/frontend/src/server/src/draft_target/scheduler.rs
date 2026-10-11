// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Ready-request batching for one public model in an immutable runtime generation.

use std::{collections::BTreeMap, pin::Pin, sync::Arc};

use foretoken_llm_facade::{LlmFacadeError, LlmFacadeResolver, TokenStream};
use foretoken_router::{RouteDecision, Router};
use futures::{FutureExt, Stream, StreamExt, stream::SelectAll};
use tokio::sync::{mpsc, oneshot, watch};
use tokio_util::sync::CancellationToken;

use super::{DraftTargetRequestState, DraftTargetRound, select_draft_target_round};
use crate::GenerationError;

/// Runtime-owned ready queue; each response retains its own cancellation and backpressure.
pub struct DraftTargetScheduler {
    incoming: mpsc::Sender<Entry>,
    // Worker refreshes notify waiters; true permanently retires fresh admission to this runtime.
    updates: watch::Sender<bool>,
}

struct Destination {
    first_route: Option<oneshot::Sender<Result<Option<RouteDecision>, GenerationError>>>,
    output: mpsc::Sender<Result<vllm_llm::GenerateOutput, LlmFacadeError>>,
}

struct Entry {
    state: DraftTargetRequestState,
    destination: Destination,
}

enum QueueEvent {
    Ready(Box<Entry>),
    Waiting(Vec<Entry>),
    Finished,
}

type ReadyStream = Pin<Box<dyn Stream<Item = QueueEvent> + Send>>;

impl DraftTargetScheduler {
    /// Starts a model queue. Runtime responses retain its owner; dropping the owner closes the queue.
    pub fn new(
        model: String,
        router: Arc<dyn Router>,
        resolver: Arc<dyn LlmFacadeResolver>,
    ) -> Self {
        let (incoming, receiver) = mpsc::channel(1);
        let (updates, changed) = watch::channel(false);
        tokio::spawn(run(model, router, resolver, receiver, changed));
        Self { incoming, updates }
    }

    /// Wakes queued placement after the owning runtime refreshes worker observations.
    pub(crate) fn refresh(&self) {
        self.updates.send_modify(|_| {});
    }

    /// Returns unadmitted queued requests to the HTTP owner for the newly published runtime.
    pub(crate) fn retire(&self) {
        self.updates.send_replace(true);
    }

    /// Enqueues normalized input and returns its first actual Target route and output stream.
    /// None asks the HTTP owner to reprepare an unexecuted request in the latest runtime.
    /// Dropping admission or the returned stream cancels this request, including between rounds.
    pub async fn generate(
        &self,
        request: vllm_llm::GenerateRequest,
    ) -> Result<Option<(RouteDecision, TokenStream)>, GenerationError> {
        let cancellation = CancellationToken::new();
        let guard = cancellation.clone().drop_guard();
        let state = DraftTargetRequestState::new(request, cancellation)?;
        let (output, mut receiver) = mpsc::channel(1);
        let (first_route, selected) = oneshot::channel();
        self.incoming
            .send(Entry {
                state,
                destination: Destination {
                    first_route: Some(first_route),
                    output,
                },
            })
            .await
            .map_err(|_| GenerationError::Unavailable)?;
        let Some(decision) = selected.await.map_err(|_| GenerationError::Internal)?? else {
            return Ok(None);
        };
        let stream = Box::pin(async_stream::stream! {
            let _guard = guard;
            while let Some(output) = receiver.recv().await {
                yield output;
            }
        });
        Ok(Some((decision, stream)))
    }
}

/// Dispatches ready members without waiting for pending prefills or slow output consumers.
async fn run(
    model: String,
    router: Arc<dyn Router>,
    resolver: Arc<dyn LlmFacadeResolver>,
    mut incoming: mpsc::Receiver<Entry>,
    mut updates: watch::Receiver<bool>,
) {
    let mut executing = SelectAll::<ReadyStream>::new();
    let mut ready = Vec::new();
    let mut waiting = Vec::new();
    let mut admission_open = true;
    loop {
        // Drain only results already ready, then form new rounds. There is no batching timer.
        while let Some(Some(event)) = executing.next().now_or_never() {
            match event {
                QueueEvent::Ready(entry) => {
                    ready.append(&mut waiting);
                    ready.push(*entry);
                }
                QueueEvent::Waiting(entries) => waiting.extend(entries),
                QueueEvent::Finished => ready.append(&mut waiting),
            }
        }
        waiting.retain(|entry| !entry.state.cancellation.is_cancelled());
        if *updates.borrow() {
            ready.extend(waiting.extract_if(.., |entry| entry.state.target_location().is_none()));
        }
        if let Ok(entry) = incoming.try_recv() {
            ready.push(entry);
        }
        let mut groups = BTreeMap::<_, Vec<_>>::new();
        for mut entry in ready.drain(..) {
            if entry.state.cancellation.is_cancelled() {
                continue;
            }
            if *updates.borrow() && entry.state.target_location().is_none() {
                if let Some(first) = entry.destination.first_route.take() {
                    let _ = first.send(Ok(None));
                }
                continue;
            }
            let requirement = DraftTargetRound::required_target(std::slice::from_ref(&entry.state))
                .expect("one member has one Target requirement")
                .map(|route| (route.route_target_id, route.data_parallel_rank));
            // Random sampling requires probability-bearing proposals. Keep it separate from
            // greedy requests that may only have token-only workers available.
            let random = entry.state.input.sampling.temperature != 0.0;
            groups
                .entry((requirement, random, entry.state.closed_workers.clone()))
                .or_default()
                .push(entry);
        }
        for entries in groups.into_values() {
            executing.push(execute_batch(
                model.clone(),
                router.clone(),
                resolver.clone(),
                entries,
            ));
        }
        if !admission_open && executing.is_empty() && waiting.is_empty() {
            break;
        }
        // Subscribe before placement and retain the watch version while it runs: a refresh
        // racing selection must still wake the queue. Cancellation does not require a refresh.
        let cancelled = waiting
            .iter()
            .map(|entry| {
                let token = entry.state.cancellation.clone();
                async move { token.cancelled().await }
            })
            .collect::<futures::stream::FuturesUnordered<_>>();
        tokio::pin!(cancelled);
        tokio::select! {
            _ = cancelled.next(), if !waiting.is_empty() => {},
            changed = updates.changed() => {
                if changed.is_err() { break; }
                ready.append(&mut waiting);
            }
            entry = incoming.recv(), if admission_open => match entry {
                Some(entry) => ready.push(entry),
                None => admission_open = false,
            },
            event = executing.next(), if !executing.is_empty() => {
                match event {
                    Some(QueueEvent::Ready(entry)) => {
                        ready.append(&mut waiting);
                        ready.push(*entry);
                    }
                    Some(QueueEvent::Waiting(entries)) => waiting.extend(entries),
                    Some(QueueEvent::Finished) => ready.append(&mut waiting),
                    None => {},
                }
            },
        }
    }
}

/// Owns one round's destinations until output delivery transfers each continuation back to ready.
fn execute_batch(
    model: String,
    router: Arc<dyn Router>,
    resolver: Arc<dyn LlmFacadeResolver>,
    entries: Vec<Entry>,
) -> ReadyStream {
    let cancellations = entries
        .iter()
        .map(|entry| entry.state.cancellation.clone())
        .collect::<Vec<_>>();
    let batch = async_stream::stream! {
        let mut destinations = BTreeMap::new();
        let mut members = Vec::new();
        for entry in entries {
            destinations.insert(entry.state.request_id().to_owned(), entry.destination);
            members.push(entry.state);
        }
        let round = select_draft_target_round(router.as_ref(), resolver.as_ref(), &model, &mut members).await;
        let mut deliveries = futures::stream::FuturesUnordered::new();
        match round {
            Err(error) => {
                for destination in destinations.into_values() {
                    deliveries.push(deliver(destination, Err(error), None));
                }
            }
            Ok(None) => {
                yield QueueEvent::Waiting(members.into_iter().map(|state| {
                    let destination = destinations.remove(state.request_id()).expect("queued member");
                    Entry { state, destination }
                }).collect());
            }
            Ok(Some(round)) => {
                let target = round.route.as_ref().expect("router-selected round").decisions().1.clone();
                let mut results = round.execute();
                // Delivery futures run beside verification; one stalled client cannot hold
                // another member's result or the native scheduler's active slots.
                loop {
                    tokio::select! {
                        Some(entry) = deliveries.next(), if !deliveries.is_empty() => {
                            if let Some(entry) = entry { yield QueueEvent::Ready(Box::new(entry)); }
                        }
                        result = results.next() => match result {
                            Some(Ok(event)) => {
                                let mut destination = destinations.remove(&event.request_id).expect("round member");
                                // Refusal never fixes the public response to an unadmitted Target.
                                if event.output.is_some() && let Some(first) = destination.first_route.take() {
                                    let _ = first.send(Ok(Some(target.clone())));
                                }
                                deliveries.push(deliver(destination, Ok(event.output), event.continuation));
                            }
                            Some(Err(error)) => {
                                for destination in std::mem::take(&mut destinations).into_values() {
                                    deliveries.push(deliver(destination, Err(error.into()), None));
                                }
                                break;
                            }
                            None => break,
                        }
                    }
                }
                yield QueueEvent::Finished;
            }
        }
        while let Some(entry) = deliveries.next().await {
            if let Some(entry) = entry { yield QueueEvent::Ready(Box::new(entry)); }
        }
    };
    let cancelled = futures::future::join_all(
        cancellations
            .into_iter()
            .map(|token| async move { token.cancelled().await }),
    );
    Box::pin(batch.take_until(cancelled))
}

/// Applies per-request output backpressure before making its continuation schedulable again.
async fn deliver(
    mut destination: Destination,
    output: Result<Option<vllm_llm::GenerateOutput>, GenerationError>,
    continuation: Option<DraftTargetRequestState>,
) -> Option<Entry> {
    let output = match output {
        // Admission refusal returns state without tokens; cancellation returns neither.
        Ok(None) => return continuation.map(|state| Entry { state, destination }),
        Ok(Some(output)) => Ok(output),
        Err(error) => {
            if let Some(first) = destination.first_route.take() {
                let _ = first.send(Err(error));
                return None;
            }
            Err(match error {
                GenerationError::InvalidRequest => LlmFacadeError::InvalidRequest,
                GenerationError::Unavailable => LlmFacadeError::Unavailable,
                GenerationError::BackendRejected => LlmFacadeError::Rejected,
                GenerationError::BackendProtocol => LlmFacadeError::Protocol,
                GenerationError::Internal => LlmFacadeError::Configuration,
                _ => LlmFacadeError::RequestFailed,
            })
        }
    };
    destination.output.send(output).await.ok()?;
    continuation.map(|state| Entry { state, destination })
}
