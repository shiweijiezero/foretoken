// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Detached engine execution and ledger termination independent of HTTP output delivery.

mod admission;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use futures::StreamExt;

use crate::backend::{
    Backend, BackendError, ExecutionControl, GenerateInput, TokenEvent, TokenStream,
};
pub use admission::{
    AdmissionPermit, AdmissionRejection, InstanceAdmissionConfig, InstanceAdmissionStatus,
    InstanceAdmissionWatcher, RuntimeHealth,
};

/// Submission failures mapped by transport adapters before output delivery begins.
#[derive(Debug, Clone, Copy)]
pub(crate) enum ExecutionError {
    AdmissionUnavailable,
    Backend(BackendError),
}

/// Ledger identity retained by execution tasks independently of HTTP output.
#[derive(Clone)]
pub(crate) struct ExecutionStore {
    pub(crate) ledger: Arc<foretoken_request_ledger::RequestLedger>,
    pub(crate) pod_uid: String,
    pub(crate) epoch: u64,
}

/// Owns external request identities until their accepted execution has terminated.
#[derive(Clone, Default)]
pub(crate) struct ExecutionRegistry {
    requests: Arc<Mutex<BTreeMap<String, Arc<ExecutionControl>>>>,
}

/// Signals output-consumer cancellation to the independent execution task.
struct CancelExecution(Arc<ExecutionControl>);

/// Keeps the external request ID cancellable through preparation and native termination.
struct ExecutionRegistration {
    request_id: String,
    registry: ExecutionRegistry,
}

/// Submission or termination evidence used to settle one stage's ledger reservation.
#[derive(Clone, Copy)]
enum ReservationEnd {
    NotSubmitted,
    StageTerminated,
    RequestTerminated,
}

impl ExecutionRegistry {
    /// Signals registered work without treating cancellation as native termination.
    pub(crate) fn cancel(&self, request_ids: &[String]) {
        let requests = self
            .requests
            .lock()
            .expect("execution registry lock poisoned");
        for request_id in request_ids {
            if let Some(execution) = requests.get(request_id) {
                execution.cancel();
            }
        }
    }

    /// Reserves one request identity and returns the execution-owned removal guard.
    fn register(
        &self,
        request_id: String,
        execution: Arc<ExecutionControl>,
    ) -> Result<ExecutionRegistration, ExecutionError> {
        let mut requests = self
            .requests
            .lock()
            .expect("execution registry lock poisoned");
        if requests.contains_key(&request_id) {
            return Err(ExecutionError::Backend(BackendError::Rejected));
        }
        requests.insert(request_id.clone(), execution);
        Ok(ExecutionRegistration {
            request_id,
            registry: self.clone(),
        })
    }
}

/// Transfers accepted work to execution tasks and returns output to the transport adapter.
/// Dropping the caller cannot drop submission; permits remain held until termination is established.
pub(crate) async fn start_execution(
    backend: Arc<dyn Backend>,
    health: Arc<RuntimeHealth>,
    ledger: Option<ExecutionStore>,
    executions: ExecutionRegistry,
    mut input: GenerateInput,
    permit: AdmissionPermit,
) -> Result<TokenStream, ExecutionError> {
    let request_id = input.request_id.clone();
    let execution = Arc::new(ExecutionControl::default());
    let registration = executions.register(request_id.clone(), execution.clone())?;
    let (mut reply, received) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let owner = execution.owner.clone();
        let shared = match input.reservation.take() {
            Some(reference) => match ledger {
                Some(store) => Some((store, reference)),
                None => {
                    let _ = reply.send(Err(ExecutionError::Backend(BackendError::Unavailable)));
                    return;
                }
            },
            None => None,
        };
        if let Some((store, reference)) = &shared {
            loop {
                match store
                    .ledger
                    .accept(reference, &owner, &store.pod_uid, store.epoch)
                    .await
                {
                    Ok(foretoken_request_ledger::Outcome::Applied) => break,
                    Ok(foretoken_request_ledger::Outcome::BackendUnavailable) => {
                        let _ = reply.send(Err(ExecutionError::AdmissionUnavailable));
                        return;
                    }
                    Ok(_) => {
                        drop(permit);
                        finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
                        let _ =
                            reply.send(Err(ExecutionError::Backend(BackendError::InvalidRequest)));
                        return;
                    }
                    Err(_) if reply.is_closed() || execution.is_cancelled() => {
                        let _ = reply.send(Err(ExecutionError::Backend(BackendError::Rejected)));
                        drop(permit);
                        finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
                        return;
                    }
                    Err(_) => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
                }
            }
        }
        if reply.is_closed() || execution.is_cancelled() {
            let _ = reply.send(Err(ExecutionError::Backend(BackendError::Rejected)));
            drop(permit);
            finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
            return;
        }
        // Cancellation can stop preparation, but a started submission retains its completion owner.
        let submission = {
            let generation = backend.generate(input, execution.clone());
            tokio::pin!(generation);
            let cancellation = async {
                tokio::select! {
                    () = execution_cancelled(&execution, &shared) => {},
                    () = reply.closed() => {},
                }
            };
            tokio::pin!(cancellation);
            tokio::select! {
                biased;
                () = health.wait_stopped() => Err(BackendError::Unavailable),
                () = &mut cancellation => {
                    execution.cancel();
                    tokio::select! {
                        result = &mut generation => result,
                        () = health.wait_stopped() => Err(BackendError::Unavailable),
                    }
                },
                result = &mut generation => result,
            }
        };
        let generation = match submission {
            Ok(generation) => generation,
            Err(error) => {
                let _ = reply.send(Err(ExecutionError::Backend(error)));
                let end = if !execution.is_submitted()
                    || matches!(error, BackendError::InvalidRequest | BackendError::Rejected)
                {
                    ReservationEnd::NotSubmitted
                } else {
                    stop_uncertain_execution(&health).await;
                    ReservationEnd::RequestTerminated
                };
                drop(permit);
                finish_shared(&shared, &owner, end).await;
                return;
            }
        };
        let cancellation = execution.clone();
        let (completed, mut completion) = tokio::sync::watch::channel(false);
        let crate::backend::BackendGeneration {
            mut stream,
            completion: mut engine_completion,
        } = generation;
        tokio::spawn(async move {
            let _registration = registration;
            let result = tokio::select! {
                result = &mut engine_completion => result,
                () = health.wait_stopped() => Err(BackendError::Unavailable),
                () = execution_cancelled(&cancellation, &shared) => {
                    cancellation.cancel();
                    let ids = [request_id];
                    let abort = tokio::select! {
                        result = backend.abort(&ids) => result,
                        () = health.wait_stopped() => Err(BackendError::Unavailable),
                    };
                    if abort.is_err() { health.fail_execution(); }
                    tokio::select! {
                        result = &mut engine_completion => result,
                        () = health.wait_stopped() => Err(BackendError::Unavailable),
                    }
                },
            };
            // Register remote cleanup before releasing the permit so drain cannot miss the handoff.
            drop(engine_completion);
            if result.is_err() {
                stop_uncertain_execution(&health).await;
            }
            drop(permit);
            // Final output can finish while persistence recovers. Intermediate output waits for
            // the ownership handoff, because the next engine must claim the same caller slot.
            let handoff = result.is_ok()
                && shared
                    .as_ref()
                    .is_some_and(|(_, reference)| !reference.final_stage);
            if !handoff {
                completed.send_replace(true);
            }
            let end = if result.is_ok() {
                ReservationEnd::StageTerminated
            } else {
                ReservationEnd::RequestTerminated
            };
            finish_shared(&shared, &owner, end).await;
            if handoff {
                completed.send_replace(true);
            }
        });
        let cleanup = CancelExecution(execution);
        let output: crate::backend::TokenStream = Box::pin(async_stream::stream! {
            let _cleanup = cleanup;
            while let Some(item) = stream.next().await {
                let terminal = match &item {
                    Ok(TokenEvent::Token(output)) => output.finish_reason.is_some(),
                    Ok(TokenEvent::Error { .. }) | Err(_) => true,
                };
                if terminal && completion.wait_for(|complete| *complete).await.is_err() {
                    yield Err(BackendError::Unavailable);
                    return;
                }
                yield item;
                if terminal { return; }
            }
        });
        let _ = reply.send(Ok(output));
    });
    received
        .await
        .map_err(|_| ExecutionError::Backend(BackendError::RequestFailed))?
}

/// Observe local and shared-ledger cancellation throughout preparation and execution.
async fn execution_cancelled(
    execution: &ExecutionControl,
    shared: &Option<(ExecutionStore, foretoken_request_ledger::ReservationRef)>,
) {
    let remote_cancellation = async {
        if let Some((store, reference)) = shared {
            loop {
                match store.ledger.wait_cancelled(reference).await {
                    Ok(()) => return,
                    Err(_) => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
                }
            }
        }
        std::future::pending::<()>().await;
    };
    tokio::select! {
        () = execution.cancelled() => {},
        () = remote_cancellation => {},
    }
}

impl Drop for ExecutionRegistration {
    fn drop(&mut self) {
        self.registry
            .requests
            .lock()
            .expect("execution registry lock poisoned")
            .remove(&self.request_id);
    }
}

impl Drop for CancelExecution {
    fn drop(&mut self) {
        self.0.cancel();
    }
}

/// Resolve unknown native completion through supervisor-confirmed process termination.
async fn stop_uncertain_execution(health: &RuntimeHealth) {
    health.fail_execution();
    health.wait_stopped().await;
}

/// Persist the execution task's terminal evidence, retaining reservations across ledger outages.
async fn finish_shared(
    shared: &Option<(ExecutionStore, foretoken_request_ledger::ReservationRef)>,
    owner: &str,
    end: ReservationEnd,
) {
    let Some((store, reference)) = shared else {
        return;
    };
    let mut reference = reference.clone();
    reference.final_stage |= matches!(end, ReservationEnd::RequestTerminated);
    loop {
        let result = match end {
            ReservationEnd::NotSubmitted => {
                store
                    .ledger
                    .reject_before_submission(&reference, owner)
                    .await
            }
            ReservationEnd::StageTerminated | ReservationEnd::RequestTerminated => {
                store.ledger.complete(&reference, owner).await
            }
        };
        match result {
            Ok(()) => return,
            Err(_) => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
        }
    }
}
