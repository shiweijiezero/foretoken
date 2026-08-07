// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use crate::{RouteContext, RouteFilter, RouteOptionCandidate, RouteOptionKind};

/// 仅保留用户允许的完整路由拓扑，不承担流控或容量判断。
pub struct SimpleFilter {
    allowed_kinds: Vec<RouteOptionKind>,
}

impl SimpleFilter {
    pub fn new(allowed_kinds: impl IntoIterator<Item = RouteOptionKind>) -> Self {
        Self {
            allowed_kinds: allowed_kinds.into_iter().collect(),
        }
    }
}

impl RouteFilter for SimpleFilter {
    fn allows(&self, option: &RouteOptionCandidate, _: RouteContext<'_>) -> bool {
        self.allowed_kinds.contains(&option.kind)
    }
}
