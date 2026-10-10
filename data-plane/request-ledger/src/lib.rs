// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared request reservations transferred from frontend waiting to backend execution.

use redis::aio::{ConnectionManager, ConnectionManagerConfig};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use thiserror::Error;
use tokio::sync::OnceCell;

const MODEL_KEY_PREFIX: &str = "foretoken:requests:";
static TRANSITION: std::sync::LazyLock<redis::Script> =
    std::sync::LazyLock::new(|| redis::Script::new(include_str!("transition.lua")));

/// Internal reference to one candidate's capacity; it carries no credential or caller identity.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReservationRef {
    pub scope: String,
    pub model: String,
    pub request: String,
    pub slot: u32,
    pub final_stage: bool,
}

/// Redis-compatible atomic ledger shared by frontend replicas and execution owners.
/// Connections are opened on demand so unrestricted services need no request-path store access.
pub struct RequestLedger {
    client: redis::Client,
    connection: OnceCell<ConnectionManager>,
}

/// A reservation transition's domain outcome, distinct from storage failure.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Outcome {
    Applied,
    Full,
    Busy,
    InvalidRole,
    BatchTooLarge,
    Closed,
    AlreadyAccepted,
    BackendUnavailable,
}

/// Storage and protocol failures prevent new bounded reservations.
#[derive(Debug, Error)]
pub enum LedgerError {
    #[error("invalid request capacity store configuration")]
    Configuration,
    #[error("request capacity store is unavailable: {0}")]
    Storage(#[from] redis::RedisError),
    #[error("request capacity store has no published model configuration")]
    Unpublished,
    #[error("frontend execution owner is no longer active")]
    OwnerClosed,
    #[error("unexpected request capacity response: {0}")]
    Protocol(String),
}

impl RequestLedger {
    /// Validates the configured store address without connecting or exposing it in diagnostics.
    pub fn new(url: &str) -> Result<Self, LedgerError> {
        Ok(Self {
            client: redis::Client::open(url).map_err(|_| LedgerError::Configuration)?,
            connection: OnceCell::new(),
        })
    }

    /// Resolves the controller-projected store without retaining credentials in application config.
    pub fn from_env() -> Result<Option<Self>, LedgerError> {
        match std::env::var("FORETOKEN_ADMISSION_STORE_URL") {
            Ok(url) if !url.is_empty() => Self::new(&url).map(Some),
            Err(std::env::VarError::NotPresent) => Ok(None),
            _ => Err(LedgerError::Configuration),
        }
    }

    // Length-prefixed components keep model and service identities distinct without content hashes.
    fn key(scope: &str, model: &str) -> String {
        format!("{MODEL_KEY_PREFIX}{}:{scope}:{model}", scope.len())
    }

    /// Shares the upstream client with bounded connection attempts and short reconnect sleeps.
    /// Connection deadlines cover stale service endpoints; retry jitter adds up to one second.
    async fn connection(&self) -> Result<ConnectionManager, LedgerError> {
        let connection = self
            .connection
            .get_or_try_init(|| {
                self.client.get_connection_manager_with_config(
                    ConnectionManagerConfig::new()
                        .set_connection_timeout(std::time::Duration::from_secs(5))
                        .set_max_delay(1_000),
                )
            })
            .await?;
        Ok(connection.clone())
    }

    async fn transition(
        &self,
        scope: &str,
        model: &str,
        action: &str,
        request: &str,
        input: Value,
    ) -> Result<String, LedgerError> {
        let mut connection = self.connection().await?;
        Ok(TRANSITION
            .key(Self::key(scope, model))
            .arg(action)
            .arg(request)
            .arg(input.to_string())
            .invoke_async(&mut connection)
            .await?)
    }

    fn outcome(value: String) -> Result<Outcome, LedgerError> {
        match value.as_str() {
            "ok" => Ok(Outcome::Applied),
            "full" => Ok(Outcome::Full),
            "busy" => Ok(Outcome::Busy),
            "role" => Ok(Outcome::InvalidRole),
            "batch" => Ok(Outcome::BatchTooLarge),
            "closed" => Ok(Outcome::Closed),
            "accepted" => Ok(Outcome::AlreadyAccepted),
            "backend_unavailable" => Ok(Outcome::BackendUnavailable),
            "unavailable" => Err(LedgerError::Unpublished),
            _ => Err(LedgerError::Protocol(value)),
        }
    }

    /// Publishes effective settings monotonically; older frontend snapshots cannot reset limits.
    pub async fn publish(
        &self,
        scope: &str,
        model: &str,
        version: u64,
        config: Value,
        frontend_instances: &[String],
        backend_instances: &[String],
    ) -> Result<(), LedgerError> {
        Self::outcome(
            self.transition(
                scope,
                model,
                "publish",
                "",
                json!({"version": version, "config": config, "frontends": frontend_instances, "backends": backend_instances}),
            )
            .await?,
        )?;
        Ok(())
    }

    /// Publishes authoritative Pod membership without changing the model's active admission rules.
    /// Snapshot observation uses this even when candidate serving settings cannot be activated.
    pub async fn publish_membership(
        &self,
        scope: &str,
        model: &str,
        version: u64,
        frontend_instances: &[String],
        backend_instances: &[String],
    ) -> Result<(), LedgerError> {
        Self::outcome(
            self.transition(
                scope,
                model,
                "publish_membership",
                "",
                json!({"version": version, "frontends": frontend_instances, "backends": backend_instances}),
            )
            .await?,
        )?;
        Ok(())
    }

    /// Registers a fresh engine process before it accepts traffic and retires its stopped predecessor.
    /// Pod identity is supplied by Kubernetes; store-issued epochs fence delayed recovery commands.
    pub async fn register_backend(&self, pod: &str) -> Result<u64, LedgerError> {
        let mut connection = self.connection().await?;
        let epoch: u64 = redis::cmd("INCR")
            .arg(format!("foretoken:engine-epoch:{pod}"))
            .query_async(&mut connection)
            .await?;
        let mut cursor = 0_u64;
        loop {
            let (next, keys): (u64, Vec<String>) = redis::cmd("SCAN")
                .arg(cursor)
                .arg("MATCH")
                .arg(format!("{MODEL_KEY_PREFIX}*"))
                .query_async(&mut connection)
                .await?;
            for key in keys {
                let result: String = TRANSITION
                    .key(key)
                    .arg("recover_backend")
                    .arg("")
                    .arg(json!({"pod": pod, "epoch": epoch}).to_string())
                    .invoke_async(&mut connection)
                    .await?;
                Self::outcome(result)?;
            }
            cursor = next;
            if cursor == 0 {
                break;
            }
        }
        Ok(epoch)
    }

    /// Starts a process ownership epoch, reclaiming only its predecessor's unsubmitted work.
    /// Allocation precedes activation so delayed messages from a stopped process cannot take over.
    pub async fn register_frontend(
        &self,
        scope: &str,
        model: &str,
        frontend: &str,
    ) -> Result<u64, LedgerError> {
        let allocated = self
            .transition(
                scope,
                model,
                "allocate_owner",
                "",
                json!({"frontend": frontend}),
            )
            .await?;
        if allocated == "closed" {
            return Err(LedgerError::OwnerClosed);
        }
        let epoch: u64 = allocated.parse().map_err(|_| LedgerError::Unpublished)?;
        let outcome = Self::outcome(
            self.transition(
                scope,
                model,
                "register_owner",
                "",
                json!({"frontend": frontend, "epoch": epoch}),
            )
            .await?,
        )?;
        if outcome != Outcome::Applied {
            return Err(LedgerError::OwnerClosed);
        }
        Ok(epoch)
    }

    /// Reads authoritative shared waiting and outstanding units for observation, not scheduling.
    pub async fn occupancy(&self, scope: &str, model: &str) -> Result<(u64, u64), LedgerError> {
        let value = self
            .transition(scope, model, "stats", "", json!({}))
            .await?;
        if value == "unavailable" {
            return Err(LedgerError::Unpublished);
        }
        serde_json::from_str(&value).map_err(|error| LedgerError::Protocol(error.to_string()))
    }

    /// Atomically checks model and caller waiting capacity for the complete candidate batch.
    pub async fn enqueue(
        &self,
        reservation: &ReservationRef,
        frontend: &str,
        epoch: u64,
        caller: &str,
        role: &str,
        units: u32,
    ) -> Result<Outcome, LedgerError> {
        Self::outcome(
            self.transition(
                &reservation.scope,
                &reservation.model,
                "enqueue",
                &reservation.request,
                json!({"frontend": frontend, "epoch": epoch, "caller": caller, "role": role, "units": units}),
            )
            .await?,
        )
    }

    /// Fences an enqueue whose transport result is unknown, including cancellation arriving first.
    /// The negative record lasts only for the frontend's current ownership epoch.
    pub async fn cancel_entry(
        &self,
        reservation: &ReservationRef,
        frontend: &str,
        epoch: u64,
        caller: &str,
    ) -> Result<(), LedgerError> {
        Self::outcome(
            self.transition(
                &reservation.scope,
                &reservation.model,
                "cancel_entry",
                &reservation.request,
                json!({"frontend": frontend, "epoch": epoch, "caller": caller}),
            )
            .await?,
        )?;
        Ok(())
    }

    /// Reserves all undispatched candidates without giving the ledger ownership of queue order.
    pub async fn dispatch(&self, reservation: &ReservationRef) -> Result<Outcome, LedgerError> {
        Self::outcome(
            self.transition(
                &reservation.scope,
                &reservation.model,
                "dispatch",
                &reservation.request,
                json!({}),
            )
            .await?,
        )
    }

    /// Returns dispatch reservations only while no candidate has been accepted by a backend.
    pub async fn retry(&self, reservation: &ReservationRef) -> Result<Outcome, LedgerError> {
        Self::outcome(
            self.transition(
                &reservation.scope,
                &reservation.model,
                "retry",
                &reservation.request,
                json!({}),
            )
            .await?,
        )
    }

    /// Transfers one reserved candidate to its backend execution owner before engine submission.
    pub async fn accept(
        &self,
        reservation: &ReservationRef,
        owner: &str,
        backend_pod: &str,
        backend_epoch: u64,
    ) -> Result<Outcome, LedgerError> {
        Self::outcome(
            self.transition(
                &reservation.scope,
                &reservation.model,
                "accept",
                &reservation.request,
                json!({"slot": reservation.slot, "owner": owner, "backend_pod": backend_pod, "backend_epoch": backend_epoch}),
            )
            .await?,
        )
    }

    /// Releases an unsubmitted candidate or marks accepted work for cancellation without freeing it.
    pub async fn cancel(&self, reservation: &ReservationRef) -> Result<(), LedgerError> {
        Self::outcome(
            self.transition(
                &reservation.scope,
                &reservation.model,
                "drop",
                &reservation.request,
                json!({"slot": reservation.slot}),
            )
            .await?,
        )?;
        Ok(())
    }

    /// Fences an abandoned claim before engine submission, including a claim whose reply was lost.
    pub async fn reject_before_submission(
        &self,
        reservation: &ReservationRef,
        owner: &str,
    ) -> Result<(), LedgerError> {
        Self::outcome(
            self.transition(
                &reservation.scope,
                &reservation.model,
                "reject_before_submission",
                &reservation.request,
                json!({"slot": reservation.slot, "owner": owner}),
            )
            .await?,
        )?;
        Ok(())
    }

    /// Releases a candidate after its execution owner has confirmed terminal engine state.
    pub async fn complete(
        &self,
        reservation: &ReservationRef,
        owner: &str,
    ) -> Result<(), LedgerError> {
        Self::outcome(self.transition(&reservation.scope, &reservation.model, "complete", &reservation.request,
            json!({"slot": reservation.slot, "owner": owner, "final_stage": reservation.final_stage})).await?)?;
        Ok(())
    }

    /// Lets the execution owner observe cancellation independently of an HTTP response consumer.
    pub async fn cancelled(&self, reservation: &ReservationRef) -> Result<bool, LedgerError> {
        let state = self
            .transition(
                &reservation.scope,
                &reservation.model,
                "status",
                &reservation.request,
                json!({"slot": reservation.slot}),
            )
            .await?;
        match state.as_str() {
            "cancelling" | "closed" | "completed" => Ok(true),
            "accepted" | "reserved" | "waiting" | "handoff" => Ok(false),
            "unavailable" => Err(LedgerError::Unpublished),
            _ => Err(LedgerError::Protocol(state)),
        }
    }
}
