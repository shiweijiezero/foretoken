// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! 可供用户参考和扩展的 Router policy 实现。

pub mod filter;
pub mod picker;
pub mod scorer;

use std::sync::Arc;

use crate::{
    RouteContext, RouteOptionCandidate, RouteOptionKind, RouteScore, RouterPolicy,
    ScoredRouteOption,
};

pub use filter::SimpleFilter;
pub use picker::SimplePicker;
pub use scorer::SimpleScorer;

/// 在核心已验证的完整 route option 上表达额外硬约束。
pub trait RouteFilter: Send + Sync {
    fn allows(&self, option: &RouteOptionCandidate, context: RouteContext<'_>) -> bool;
}

/// 对通过过滤的完整 route option 计算软分数。
pub trait RouteScorer: Send + Sync {
    fn score(&self, option: &RouteOptionCandidate, context: RouteContext<'_>) -> RouteScore;
}

/// 对评分后的 option 排序，不负责删除候选或预留容量。
pub trait RoutePicker: Send + Sync {
    fn order(
        &self,
        options: &[ScoredRouteOption],
        context: RouteContext<'_>,
        turn: usize,
    ) -> Vec<usize>;
}

/// 组装一个简单的自定义 Router policy。
pub fn simple_policy(allowed_kinds: impl IntoIterator<Item = RouteOptionKind>) -> RouterPolicy {
    RouterPolicy::new(
        Arc::new(SimpleFilter::new(allowed_kinds)),
        Arc::new(SimpleScorer),
        Arc::new(SimplePicker),
    )
}
