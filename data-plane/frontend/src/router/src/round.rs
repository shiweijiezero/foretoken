// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! One round's selected DT workers and per-member reservation ownership.

use crate::routing_load::RoutingReservations;
use crate::{RouteCandidate, RouteDecision, RouteTargetId};
use std::collections::BTreeSet;
use std::sync::{Arc, Mutex};

/// Executor-owned placement constraints for a DT batch; these are not client sampling options.
#[derive(Default)]
pub struct DraftTargetPlacement {
    /// Target that already owns the members' sessions/KV, if any.
    pub required_target: Option<RouteDecision>,
    /// Original Target metadata for a live session, even after discovery removes its route.
    pub retained_target: Option<RouteCandidate>,
    /// Workers that explicitly refused a member's new session during drain.
    pub closed_workers: BTreeSet<RouteTargetId>,
}

/// DT placement retained until each member returns to the caller or the round drops.
pub struct DraftTargetRoundRoute {
    target: RouteDecision,
    target_candidate: RouteCandidate,
    draft: Option<RouteDecision>,
    members: BTreeSet<String>,
    load: Arc<Mutex<RoutingReservations>>,
}

impl DraftTargetRoundRoute {
    /// Retains Target reservations while Draft selection is still fallible.
    pub(crate) fn new(
        target_candidate: RouteCandidate,
        members: BTreeSet<String>,
        load: Arc<Mutex<RoutingReservations>>,
    ) -> Self {
        Self {
            target: target_candidate.decision(),
            target_candidate,
            draft: None,
            members,
            load,
        }
    }

    /// Transfers the selected Draft reservations into the same round owner.
    pub(crate) fn set_draft(&mut self, draft: RouteDecision) {
        self.draft = Some(draft);
    }

    /// Retains Target compatibility metadata across inventory replacement for an owned session.
    pub fn target_candidate(&self) -> &RouteCandidate {
        &self.target_candidate
    }

    /// Returns the workers selected for this round.
    pub fn decisions(&self) -> (&RouteDecision, &RouteDecision) {
        (
            self.draft.as_ref().expect("round route has selected Draft"),
            &self.target,
        )
    }

    /// Releases both worker reservations before a member returns to the caller.
    pub fn finish(&mut self, request_id: &str) {
        if self.members.remove(request_id) {
            let mut load = self.load.lock().expect("routing load lock poisoned");
            for decision in std::iter::once(&self.target).chain(self.draft.iter()) {
                load.release(
                    &(
                        decision.route_target_id.clone(),
                        decision.data_parallel_rank,
                    ),
                    request_id,
                );
            }
        }
    }
}

impl Drop for DraftTargetRoundRoute {
    fn drop(&mut self) {
        while let Some(id) = self.members.first().cloned() {
            self.finish(&id);
        }
    }
}
