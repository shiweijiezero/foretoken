// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use crate::{RouteContext, RouteFilter, RouteOptionCandidate};

pub struct AllowAllFilter;

impl RouteFilter for AllowAllFilter {
    fn allows(&self, _: &RouteOptionCandidate, _: RouteContext<'_>) -> bool {
        true
    }
}
