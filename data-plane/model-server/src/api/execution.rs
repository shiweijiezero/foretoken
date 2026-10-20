// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Detached engine execution and ledger termination independent of HTTP output delivery.

use std::sync::Arc;

use futures::StreamExt;

use super::{AdmissionPermit, ApiError, AppState, RuntimeHealth};
use crate::backend::{BackendError, GenerateInput, TokenEvent};

/// Ledger identity retained by execution tasks independently of HTTP output.
#[derive(Clone)]
pub(super) struct ExecutionStore {
    pub(super) ledger: Arc<foretoken_request_ledger::RequestLedger>,
    pub(super) pod_uid: String,
    pub(super) epoch: u64,
}

/// Signals output-consumer cancellation to the independent execution task.
struct CancelExecution(Arc<tokio::sync::Notify>);

/// Submission or termination evidence used to settle one stage's ledger reservation.
#[derive(Clone, Copy)]
enum ReservationEnd {
    NotSubmitted,
    StageTerminated,
    RequestTerminated,
}

/// Returns the generate handler's output stream while detached tasks retain engine execution.
/// HTTP disconnects cannot drop submission; permits remain held until termination is established.
pub(super) async fn start_execution(
    state: AppState,
    mut input: GenerateInput,
    permit: AdmissionPermit,
) -> Result<crate::backend::TokenStream, ApiError> {
    let (reply, received) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let owner = uuid::Uuid::new_v4().to_string();
        let shared = match input.reservation.take() {
            Some(reference) => match state.ledger.clone() {
                Some(store) => Some((store, reference)),
                None => {
                    let _ = reply.send(Err(ApiError::Unavailable));
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
                        let _ = reply.send(Err(ApiError::AdmissionUnavailable));
                        return;
                    }
                    Ok(_) => {
                        drop(permit);
                        finish_shared(&shared, &owner, ReservationEnd::NotSubmitted).await;
                        let _ = reply.send(Err(ApiError::InvalidRequest));
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
            () = state.health.wait_stopped() => Err(BackendError::Unavailable),
            result = state.backend.generate(input) => result,
        };
        let generation = match submission {
            Ok(generation) => generation,
            Err(error) => {
                let _ = reply.send(Err(ApiError::backend(error)));
                let end = if matches!(error, BackendError::InvalidRequest | BackendError::Rejected)
                {
                    ReservationEnd::NotSubmitted
                } else {
                    stop_uncertain_execution(&state.health).await;
                    ReservationEnd::RequestTerminated
                };
                drop(permit);
                finish_shared(&shared, &owner, end).await;
                return;
            }
        };
        let cancel = Arc::new(tokio::sync::Notify::new());
        let cancellation = cancel.clone();
        let (completed, mut completion) = tokio::sync::watch::channel(false);
        let crate::backend::BackendGeneration {
            mut stream,
            completion: mut engine_completion,
        } = generation;
        tokio::spawn(async move {
            let cancellation_requested = async {
                loop {
                    tokio::select! {
                        _ = cancellation.notified() => return,
                        _ = tokio::time::sleep(std::time::Duration::from_millis(250)) => {},
                    }
                    if let Some((store, reference)) = &shared
                        && matches!(store.ledger.cancelled(reference).await, Ok(true))
                    {
                        return;
                    }
                }
            };
            let result = tokio::select! {
                result = &mut engine_completion => result,
                () = state.health.wait_stopped() => Err(BackendError::Unavailable),
                () = cancellation_requested => {
                    let ids = [request_id];
                    let abort = tokio::select! {
                        result = state.backend.abort(&ids) => result,
                        () = state.health.wait_stopped() => Err(BackendError::Unavailable),
                    };
                    if abort.is_err() { state.health.fail_execution(); }
                    tokio::select! {
                        result = engine_completion => result,
                        () = state.health.wait_stopped() => Err(BackendError::Unavailable),
                    }
                },
            };
            if result.is_err() {
                stop_uncertain_execution(&state.health).await;
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
        let cleanup = CancelExecution(cancel);
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
    received.await.map_err(|_| ApiError::RequestFailed)?
}

impl Drop for CancelExecution {
    fn drop(&mut self) {
        self.0.notify_one();
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
