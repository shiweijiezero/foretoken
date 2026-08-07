// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use crate::{RouteContext, RoutePicker, ScoredRouteOption};

pub struct StablePicker;

impl RoutePicker for StablePicker {
    fn order(&self, options: &[ScoredRouteOption], _: RouteContext<'_>, turn: usize) -> Vec<usize> {
        let mut ranked = (0..options.len()).collect::<Vec<_>>();
        ranked.sort_by(|a, b| {
            options[*b]
                .score
                .cmp(&options[*a].score)
                .then_with(|| option_key(&options[*a]).cmp(&option_key(&options[*b])))
        });
        if ranked.len() > 1 {
            let tied = ranked
                .iter()
                .take_while(|index| options[**index].score == options[ranked[0]].score)
                .count();
            ranked[..tied].rotate_left(turn % tied);
        }
        ranked
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
