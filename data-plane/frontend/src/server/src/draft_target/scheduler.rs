// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Ready-request batching that retains Target sessions while refreshing Draft inventory.

use std::{
    collections::BTreeMap,
    pin::Pin,
    sync::{Arc, Mutex},
};

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
    updates: watch::Sender<Workers>,
}

#[derive(Clone)]
struct Workers {
    router: Arc<dyn Router>,
    resolver: Arc<dyn LlmFacadeResolver>,
    retired: bool,
}

pub(super) type FirstRoute =
    Arc<Mutex<Option<oneshot::Sender<Result<Option<RouteDecision>, GenerationError>>>>>;

/// Resolves admission exactly once, at remote Target acceptance or before execution fails.
pub(super) fn send_first_route(
    first: &FirstRoute,
    result: Result<Option<RouteDecision>, GenerationError>,
) {
    if let Some(first) = first.lock().expect("DT first route lock poisoned").take() {
        let _ = first.send(result);
    }
}

struct Destination {
    first_route: FirstRoute,
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
        let (updates, changed) = watch::channel(Workers {
            router,
            resolver,
            retired: false,
        });
        tokio::spawn(run(model, receiver, changed));
        Self { incoming, updates }
    }

    /// Wakes queued placement after the owning runtime refreshes worker observations.
    pub(crate) fn refresh(&self) {
        self.updates.send_modify(|_| {});
    }

    /// Returns unadmitted queued requests to the HTTP owner for the newly published runtime.
    pub(crate) fn retire(
        &self,
        replacement: Option<(Arc<dyn Router>, Arc<dyn LlmFacadeResolver>)>,
    ) {
        self.updates.send_modify(|workers| {
            workers.retired = true;
            if let Some((router, resolver)) = replacement {
                workers.router = router;
                workers.resolver = resolver;
            }
        });
    }

    /// Enqueues normalized input and returns its first actual Target route and output stream.
    /// None asks the HTTP owner to reprepare an unexecuted request in the latest runtime.
    /// Dropping admission or the returned stream cancels this request, including between rounds.
    pub async fn generate(
        &self,
        request: vllm_llm::GenerateRequest,
        admission: Arc<foretoken_admission::AdmissionPermit>,
        rules: foretoken_admission::DispatchRules,
    ) -> Result<Option<(RouteDecision, TokenStream)>, GenerationError> {
        let cancellation = CancellationToken::new();
        let guard = cancellation.clone().drop_guard();
        let mut state = DraftTargetRequestState::new(request, cancellation)?;
        state.input.reservation = admission.reference().map(|mut reference| {
            reference.final_stage = true;
            reference
        });
        state.input.priority = rules
            .priority
            .map_or(state.input.priority, |priority| !priority);
        state.allowed_pools = rules.allowed_pools;
        state.admission = Some(admission.clone());
        let (output, mut receiver) = mpsc::channel(1);
        let (first_route, selected) = oneshot::channel();
        let first_route = Arc::new(Mutex::new(Some(first_route)));
        state.first_route = Some(first_route.clone());
        self.incoming
            .send(Entry {
                state,
                destination: Destination {
                    first_route,
                    output,
                },
            })
            .await
            .map_err(|_| GenerationError::Unavailable)?;
        let Some(decision) =
            crate::runtime::before_waiting_deadline(admission.waiting_deadline(), async {
                selected.await.map_err(|_| GenerationError::Internal)?
            })
            .await?
        else {
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
    mut incoming: mpsc::Receiver<Entry>,
    mut updates: watch::Receiver<Workers>,
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
        if updates.borrow().retired {
            ready.extend(waiting.extract_if(.., |entry| entry.state.target_location().is_none()));
        }
        if let Ok(entry) = incoming.try_recv() {
            ready.push(entry);
        }
        let mut groups = BTreeMap::<_, Vec<_>>::new();
        for entry in ready.drain(..) {
            if entry.state.cancellation.is_cancelled() {
                continue;
            }
            if updates.borrow().retired && entry.state.target_location().is_none() {
                send_first_route(&entry.destination.first_route, Ok(None));
                continue;
            }
            let requirement = DraftTargetRound::required_target(std::slice::from_ref(&entry.state))
                .expect("one member has one Target requirement")
                .map(|route| (route.route_target_id, route.data_parallel_rank));
            // Random sampling requires probability-bearing proposals. Keep it separate from
            // greedy requests that may only have token-only workers available.
            let random = entry.state.input.sampling.temperature != 0.0;
            groups
                .entry((
                    requirement,
                    random,
                    entry.state.closed_workers.clone(),
                    entry.state.allowed_pools.clone(),
                ))
                .or_default()
                .push(entry);
        }
        let workers = updates.borrow().clone();
        for entries in groups.into_values() {
            executing.push(execute_batch(
                model.clone(),
                workers.router.clone(),
                workers.resolver.clone(),
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
                let mut waiting = Vec::new();
                for state in members {
                    let destination = destinations.remove(state.request_id()).expect("queued member");
                    if state.target_location().is_none() {
                        // Initial nonacceptance returns the grant to Admission. Established
                        // Target sessions instead wait here for a compatible Draft refresh.
                        deliveries.push(deliver(destination, Err(GenerationError::BackendBusy), None));
                    } else {
                        waiting.push(Entry { state, destination });
                    }
                }
                yield QueueEvent::Waiting(waiting);
            }
            Ok(Some(round)) => {
                let mut results = round.execute();
                let mut progressed = false;
                // Delivery futures run beside verification; one stalled client cannot hold
                // another member's result or the native scheduler's active slots.
                loop {
                    tokio::select! {
                        Some(entry) = deliveries.next(), if !deliveries.is_empty() => {
                            if let Some(entry) = entry { yield QueueEvent::Ready(Box::new(entry)); }
                        }
                        result = results.next() => match result {
                            Some(Ok(event)) => {
                                let destination = destinations.remove(&event.request_id).expect("round member");
                                progressed |= event.output.is_some();
                                if event.observation.outcome == "busy" && event.continuation.as_ref().is_some_and(|state| state.target_location().is_some()) {
                                    if let Some(state) = event.continuation {
                                        yield QueueEvent::Waiting(vec![Entry { state, destination }]);
                                    }
                                    continue;
                                }
                                if event.output.is_none() && event.continuation.as_ref()
                                    .is_some_and(|state| state.target_location().is_none()) {
                                    deliveries.push(deliver(destination, Err(GenerationError::BackendBusy), None));
                                } else {
                                    deliveries.push(deliver(destination, Ok(event.output), event.continuation));
                                }
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
                if progressed { yield QueueEvent::Finished; }
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
    destination: Destination,
    output: Result<Option<vllm_llm::GenerateOutput>, GenerationError>,
    continuation: Option<DraftTargetRequestState>,
) -> Option<Entry> {
    let output = match output {
        // Admission refusal returns state without tokens; cancellation returns neither.
        Ok(None) => return continuation.map(|state| Entry { state, destination }),
        Ok(Some(output)) => Ok(output),
        Err(error) => {
            let unadmitted = destination
                .first_route
                .lock()
                .expect("DT first route lock poisoned")
                .is_some();
            if unadmitted {
                send_first_route(&destination.first_route, Err(error));
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
