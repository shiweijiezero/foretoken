// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use crate::{RouteContext, RoutePicker, ScoredRouteOption};

/// 按分数排序，并使用组件 ID 提供确定性的同分排序。
pub struct SimplePicker;

impl RoutePicker for SimplePicker {
    fn order(&self, options: &[ScoredRouteOption], _: RouteContext<'_>, _: usize) -> Vec<usize> {
        let mut indices = (0..options.len()).collect::<Vec<_>>();
        indices.sort_by(|a, b| {
            options[*b]
                .score
                .cmp(&options[*a].score)
                .then_with(|| option_key(&options[*a]).cmp(&option_key(&options[*b])))
        });
        indices
    }
}

fn option_key(option: &ScoredRouteOption) -> Vec<&str> {
    option
        .option
        .components
        .iter()
        .map(|component| component.backend_id.as_str())
        .collect()
}
