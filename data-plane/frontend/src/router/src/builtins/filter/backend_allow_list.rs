// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::collections::BTreeSet;

use crate::{BackendId, RouteContext, RouteFilter, RouteOptionCandidate};

pub struct BackendAllowList(pub BTreeSet<BackendId>);

impl RouteFilter for BackendAllowList {
    fn allows(&self, option: &RouteOptionCandidate, _: RouteContext<'_>) -> bool {
        option
            .components
            .iter()
            .all(|component| self.0.contains(&component.backend_id))
    }
}
