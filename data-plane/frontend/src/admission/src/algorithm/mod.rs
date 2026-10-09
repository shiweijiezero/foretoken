// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Compiled admission rules and their configuration names.

// Module inclusion and descriptor registration share one list for each built-in rule.
macro_rules! declare_admission_algorithms {
    ($( $module:ident => $algorithm:ident = $name:literal ),+ $(,)?) => {
        $(
            mod $module;
            pub use $module::$algorithm;

            inventory::submit! {
                crate::AdmissionDescriptor {
                    name: $name,
                    factory: |parameters| {
                        Ok(std::sync::Arc::new($algorithm::from_parameters(parameters)?))
                    },
                }
            }
        )+
    };
}

declare_admission_algorithms! {
    allow_all => AllowAllAdmission = "allow_all",
    concurrency => ConcurrencyAdmission = "concurrency",
}
