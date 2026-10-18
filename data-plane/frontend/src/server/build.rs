// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Tracks added migration files for incremental builds of the embedded SQLx migrator.

fn main() {
    println!("cargo:rerun-if-changed=migrations");
}
