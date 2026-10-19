// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Recovers stopped response owners and reclaims API state after ledger-confirmed completion.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use foretoken_admission::ExecutionReservation;
use foretoken_request_ledger::ReservationRef;
use serde_json::json;

use super::super::error::ApiError;
use super::ResponseTasks;
use crate::runtime::GenerationError;
use crate::store::ResponseRef;

impl ResponseTasks {
    /// Settles stopped or overdue executions against the ledger and reclaims shared API state.
    pub(crate) fn maintain(self: &Arc<Self>) {
        let weak = Arc::downgrade(self);
        tokio::spawn(async move {
            loop {
                let Some(tasks) = weak.upgrade() else {
                    break;
                };
                if let (Some(store), Some(ledger)) = (&tasks.store, &tasks.ledger) {
                    if let Ok(pending) = store.pending_responses().await {
                        let mut observed_owners = HashMap::new();
                        for pending in pending {
                            if pending.local_execution
                                && tasks
                                    .active
                                    .lock()
                                    .expect("response task lock poisoned")
                                    .contains_key(&pending.response.id)
                            {
                                continue;
                            }
                            let Some(reference) = pending.reservation else {
                                tracing::error!("stored response has no execution reference");
                                continue;
                            };
                            let Ok(reference) = serde_json::from_value::<ReservationRef>(reference)
                            else {
                                tracing::error!("stored response execution reference is invalid");
                                continue;
                            };
                            let mut retired = pending.process_replaced || pending.service_replaced;
                            if !retired && !pending.local_execution {
                                let key = (
                                    reference.scope.clone(),
                                    reference.model.clone(),
                                    pending.execution_pod.clone(),
                                );
                                let active = if let Some(active) = observed_owners.get(&key) {
                                    *active
                                } else {
                                    let Ok(active) = ledger
                                        .frontend_active(&reference, &pending.execution_pod)
                                        .await
                                    else {
                                        continue;
                                    };
                                    observed_owners.insert(key, active);
                                    active
                                };
                                if active && !pending.deadline_expired {
                                    continue;
                                }
                                retired = !active;
                            }
                            let execution = ExecutionReservation::new(ledger.clone(), reference);
                            let Ok(Some(started)) = store
                                .pending_started(&pending.response, &pending.execution_instance)
                                .await
                            else {
                                continue;
                            };
                            // A live process's detached preparation retains its own permit until it ends.
                            if (retired || !started) && execution.request_cancel().await.is_err() {
                                continue;
                            }
                            if started
                                && !matches!(ledger.finished(execution.reference()).await, Ok(true))
                            {
                                continue;
                            }
                            let error = if pending.deadline_expired {
                                ApiError::generation(GenerationError::DeadlineExceeded).body
                            } else {
                                json!({"code":"execution_lost","message":"The response execution process stopped"})
                            };
                            if store.fail_response(&pending.response, error).await.is_ok()
                                && execution.acknowledge().await.is_ok()
                            {
                                let _ = store.acknowledged_response(&pending.response).await;
                            }
                        }
                    }
                    if let Ok(pending) = store.pending_acknowledgements().await {
                        for (response, reference) in pending {
                            let Ok(reference) = serde_json::from_value::<ReservationRef>(reference)
                            else {
                                continue;
                            };
                            if ledger.acknowledge(&reference).await.is_ok() {
                                let _ = store.acknowledged_response(&response).await;
                            }
                        }
                    }
                    if let Ok(completed) =
                        ledger.completed_consumers(store.completion_prefix()).await
                    {
                        for (reference, consumer) in completed {
                            let encoded = consumer
                                .strip_prefix(store.completion_prefix())
                                .expect("ledger filters completion consumers");
                            let Ok(response) = serde_json::from_str::<ResponseRef>(encoded) else {
                                tracing::error!("API completion consumer is invalid");
                                continue;
                            };
                            // Absence in the API cache only releases evidence already confirmed terminal by the ledger.
                            if matches!(store.has_response(&response).await, Ok(false)) {
                                let _ = ledger.acknowledge(&reference).await;
                            }
                        }
                    }
                    if store.collect_expired().await.is_err() {
                        tracing::debug!(
                            "API state maintenance will resume when storage is available"
                        );
                    }
                }
                drop(tasks);
                tokio::time::sleep(Duration::from_secs(1)).await;
            }
        });
    }
}
