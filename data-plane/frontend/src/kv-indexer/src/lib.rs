// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//! Best-effort, source-scoped KV prefix index. This index never participates in correctness.

mod index;
mod sync;

pub use index::{Delta, KvIndex, Partition, Source, Stored, block_digest};
pub use sync::{
    KvEventSourceConfig, KvIndexDegradedReason, KvIndexState, KvIndexStatus, KvIndexer,
    KvRouteBinding, KvRuntimeConfig,
};
