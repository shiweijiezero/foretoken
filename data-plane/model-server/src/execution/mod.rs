// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Detached engine execution and ledger termination independent of HTTP output delivery.

mod admission;

use std::sync::Arc;

use futures::StreamExt;
use tokio_util::sync::CancellationToken;

use crate::backend::{Backend, BackendError, GenerateInput, TokenEvent, TokenStream};
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

/// Submission or termination evidence used to settle one stage's ledger reservation.
#[derive(Clone, Copy)]
enum ReservationEnd {
    NotSubmitted,
    StageTerminated,
    RequestTerminated,
}

/// Transfers accepted work to execution tasks and returns output to the transport adapter.
/// Dropping the caller cannot drop submission; permits remain held until termination is established.
pub(crate) async fn start_execution(
    backend: Arc<dyn Backend>,
    health: Arc<RuntimeHealth>,
    ledger: Option<ExecutionStore>,
    mut input: GenerateInput,
    permit: AdmissionPermit,
) -> Result<TokenStream, ExecutionError> {
    let (reply, received) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let owner = uuid::Uuid::new_v4().to_string();
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
                    Err(_) if reply.is_closed() => {
                        drop(permit);
                        finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
                        return;
                    }
                    Err(_) => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
                }
            }
        }
        if reply.is_closed() {
            drop(permit);
            finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
            return;
        }
        let request_id = input.request_id.clone();
        let submission = tokio::select! {
            biased;
            () = health.wait_stopped() => Err(BackendError::Unavailable),
            result = backend.generate(input) => result,
        };
        let generation = match submission {
            Ok(generation) => generation,
            Err(error) => {
                let _ = reply.send(Err(ExecutionError::Backend(error)));
                let end = if matches!(error, BackendError::InvalidRequest | BackendError::Rejected)
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
        let cancel = CancellationToken::new();
        let cancellation = cancel.clone();
        let (completed, mut completion) = tokio::sync::watch::channel(false);
        let crate::backend::BackendGeneration {
            mut stream,
            completion: mut engine_completion,
        } = generation;
        tokio::spawn(async move {
            let remote_cancellation = async {
                if let Some((store, reference)) = &shared {
                    loop {
                        match store.ledger.wait_cancelled(reference).await {
                            Ok(()) => return,
                            Err(_) => tokio::time::sleep(std::time::Duration::from_secs(1)).await,
                        }
                    }
                }
                std::future::pending::<()>().await;
            };
            let cancellation_requested = async {
                tokio::select! {
                    () = cancellation.cancelled() => {},
                    () = remote_cancellation => {},
                }
            };
            let result = tokio::select! {
                result = &mut engine_completion => result,
                () = health.wait_stopped() => Err(BackendError::Unavailable),
                () = cancellation_requested => {
                    let ids = [request_id];
                    let abort = tokio::select! {
                        result = backend.abort(&ids) => result,
                        () = health.wait_stopped() => Err(BackendError::Unavailable),
                    };
                    if abort.is_err() { health.fail_execution(); }
                    tokio::select! {
                        result = engine_completion => result,
                        () = health.wait_stopped() => Err(BackendError::Unavailable),
                    }
                },
            };
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
        let cleanup = cancel.drop_guard();
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
