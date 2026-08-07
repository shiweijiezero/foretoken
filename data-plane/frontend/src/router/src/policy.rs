// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Shared read-only policy types and Filter/Scorer/Picker composition.

use std::sync::Arc;

use crate::{BackendId, BackendRole, RouteFilter, RoutePicker, RouteScorer};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteOptionKind {
    Aggregate,
    PrefillDecode,
    EncoderPrefillDecode,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteComponentCandidate {
    pub backend_id: BackendId,
    pub role: BackendRole,
    pub domain_id: Option<String>,
    pub load: u64,
    pub uses_prefix_locality: bool,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct RouteOptionCandidate {
    pub kind: RouteOptionKind,
    pub components: Vec<RouteComponentCandidate>,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, PartialOrd, Ord)]
pub struct RouteScore {
    pub topology: i64,
    pub locality: i64,
    pub load: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ScoredRouteOption {
    pub option: RouteOptionCandidate,
    pub score: RouteScore,
}

pub struct RouterPolicy {
    pub filter: Arc<dyn RouteFilter>,
    pub scorer: Arc<dyn RouteScorer>,
    pub picker: Arc<dyn RoutePicker>,
}

impl RouterPolicy {
    pub fn new(
        filter: Arc<dyn RouteFilter>,
        scorer: Arc<dyn RouteScorer>,
        picker: Arc<dyn RoutePicker>,
    ) -> Self {
        Self {
            filter,
            scorer,
            picker,
        }
    }
}
