// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Retains request budgets and cleanup ownership through preprocessing and output backpressure.

use foretoken_admission::{AdmissionPermit, mark_request_deadline};
use foretoken_llm_facade::{LlmFacadeError, TokenStream};
use futures::StreamExt;
use tracing::Instrument;

use super::GenerationError;

/// Bounds preparation and workflow execution by the original HTTP request deadline.
///
/// Timed-out workflows drop their backend cleanup guards. Protected preprocessing retains
/// its separate permit until any non-cancelable blocking work has completed.
pub(crate) async fn before_deadline<T>(
    deadline: tokio::time::Instant,
    work: impl std::future::Future<Output = Result<T, GenerationError>>,
) -> Result<T, GenerationError> {
    tokio::select! {
        biased;
        _ = tokio::time::sleep_until(deadline) => {
            mark_request_deadline();
            Err(GenerationError::DeadlineExceeded)
        },
        result = work => {
            // Synchronous tokenization can finish without yielding to the timer.
            if tokio::time::Instant::now() >= deadline {
                mark_request_deadline();
                Err(GenerationError::DeadlineExceeded)
            } else {
                result
            }
        }
    }
}

/// Applies the original waiting budget until first backend acceptance.
/// Protected preparation retains its permit in its spawned task if this timer or HTTP cancels.
pub(super) async fn before_waiting_deadline<T>(
    deadline: Option<tokio::time::Instant>,
    work: impl std::future::Future<Output = Result<T, GenerationError>>,
) -> Result<T, GenerationError> {
    let Some(deadline) = deadline else {
        return work.await;
    };
    let result = tokio::time::timeout_at(deadline, work)
        .await
        .map_err(|_| GenerationError::QueueTimeout)?;
    // Synchronous tokenizer work may complete without yielding to the timer.
    if tokio::time::Instant::now() >= deadline {
        return Err(GenerationError::QueueTimeout);
    }
    result
}

/// Keeps preprocessing accounted for when an HTTP future is canceled.
/// Upstream image processing awaits blocking work that cannot be canceled once started;
/// a protected task retains the permit until that work finishes, without dispatching inference.
pub(super) async fn prepare_with_permit<T: Send + 'static>(
    admission: AdmissionPermit,
    work: impl std::future::Future<Output = Result<T, GenerationError>> + Send + 'static,
) -> Result<(T, AdmissionPermit), GenerationError> {
    let reserved = admission.is_reserved();
    let deadline = admission.waiting_deadline();
    let preparation = async move {
        let result = work.await?;
        Ok((result, admission))
    };
    before_waiting_deadline(deadline, async {
        if reserved {
            tokio::spawn(preparation.in_current_span())
                .await
                .map_err(|_| GenerationError::Internal)?
        } else {
            preparation.await
        }
    })
    .await
}

/// Transfers the same request deadline to the output stream owned by protocol adapters.
///
/// The task owns backend cleanup even while HTTP backpressure stops polling the returned stream.
/// A bounded channel preserves backpressure; dropping its receiver cancels the task's backend work.
pub(super) fn deadline_stream(
    mut stream: TokenStream,
    deadline: tokio::time::Instant,
) -> TokenStream {
    let (sender, mut receiver) = tokio::sync::mpsc::channel(1);
    let (expiry_sender, expiry_receiver) = tokio::sync::oneshot::channel();
    tokio::spawn(async move {
        let expired = tokio::select! {
            biased;
            _ = sender.closed() => false,
            _ = tokio::time::sleep_until(deadline) => true,
            _ = async {
                while let Some(item) = stream.next().await {
                    let finished = item.as_ref().map_or(true, |output| output.finish_reason.is_some());
                    if sender.send(item).await.is_err() || finished {
                        break;
                    }
                }
            } => false,
        };
        // Release backend work and finish without waiting for a stalled HTTP reader. Expiry has
        // a separate terminal slot so the queued token remains ordered before the timeout error.
        drop(stream);
        if expired {
            let _ = expiry_sender.send(());
        }
    }.in_current_span());
    Box::pin(async_stream::stream! {
        while let Some(item) = receiver.recv().await {
            yield item;
        }
        if expiry_receiver.await.is_ok() {
            yield Err(LlmFacadeError::RequestFailed);
        }
    })
}
