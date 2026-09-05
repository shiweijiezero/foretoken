// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Provides composable routing algorithm interfaces and implementations.

// Keep module inclusion and descriptor registration in one stage-owned list so an algorithm has one registration point.
macro_rules! declare_router_algorithms {
    (
        descriptor = $descriptor:ident;
        $( $module:ident => $algorithm:ident = $name:literal ),+ $(,)?
    ) => {
        $(
            mod $module;
            pub use $module::$algorithm;

            inventory::submit! {
                $crate::$descriptor {
                    name: $name,
                    factory: || std::sync::Arc::new($algorithm::default()),
                }
            }
        )+
    };
}

pub mod filter;
pub mod picker;
pub mod scorer;

pub use filter::{AllowAllFilter, RouteFilter};
pub use picker::{MaxPicker, RoundRobinPicker, RoutePicker};
pub use scorer::{KvLeastLoadedScorer, LeastLoadedScorer, RouteScorer, UniformScorer};
