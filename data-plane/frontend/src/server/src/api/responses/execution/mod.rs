// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Retains response tasks through transport disconnects and frontend shutdown.

mod maintenance;
mod response;

use std::collections::HashMap;
use std::sync::{Arc, Mutex};

use foretoken_request_ledger::RequestLedger;
use serde_json::Value;
use tokio::sync::{mpsc, oneshot};
use tokio_util::sync::{CancellationToken, DropGuard};
use tokio_util::task::TaskTracker;

use super::error::ApiError;
use crate::store::ApiStore;

pub(crate) enum ResponseReady {
    Stored(Value),
    Streaming,
    Collected(Value),
}

pub(crate) struct ResponseDelivery {
    pub ready: oneshot::Receiver<Result<ResponseReady, ApiError>>,
    pub events: mpsc::Receiver<Vec<Value>>,
    pub disconnect: Disconnect,
}

/// Cancels foreground work when its HTTP consumer leaves; background work has no such guard.
pub(crate) type Disconnect = Option<DropGuard>;

/// Frontend-owned response tasks, retained through HTTP disconnects and bounded shutdown drain.
pub struct ResponseTasks {
    tracker: TaskTracker,
    stopping: CancellationToken,
    active: Mutex<HashMap<String, CancellationToken>>,
    store: Option<Arc<ApiStore>>,
    ledger: Option<Arc<RequestLedger>>,
}

impl ResponseTasks {
    /// Creates frontend-owned execution and cache-maintenance state for the HTTP router.
    pub(crate) fn new(
        store: Option<Arc<ApiStore>>,
        ledger: Option<Arc<RequestLedger>>,
    ) -> Arc<Self> {
        Arc::new(Self {
            tracker: TaskTracker::new(),
            stopping: CancellationToken::new(),
            active: Mutex::new(HashMap::new()),
            store,
            ledger,
        })
    }

    /// Stops accepting tasks and waits for the existing executions and persistence to settle.
    pub async fn drain(&self) {
        self.tracker.close();
        self.tracker.wait().await;
    }

    /// Requests cancellation when the frontend's process drain budget has elapsed.
    pub fn cancel(&self) {
        self.stopping.cancel();
    }

    /// Wakes a local executor after a cancellation was committed by the API handler.
    pub(crate) fn request_cancel(&self, id: &str) {
        if let Some(token) = self
            .active
            .lock()
            .expect("response task lock poisoned")
            .get(id)
        {
            token.cancel();
        }
    }
}
