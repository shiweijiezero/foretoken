// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use crate::{AdmissionTarget, BackendId};

#[derive(Default)]
pub(crate) struct ReservationStore {
    reserved: Mutex<BTreeMap<BackendId, u64>>,
}

impl ReservationStore {
    pub(crate) fn available_load(&self, target: &AdmissionTarget) -> Option<u64> {
        if target.components.is_empty() {
            return None;
        }
        let reserved = self
            .reserved
            .lock()
            .expect("reservation mutex must not be poisoned");
        let mut load = 0_u64;
        for component in &target.components {
            let local = reserved.get(&component.component_id).copied().unwrap_or(0);
            if component.running_requests.saturating_add(local) >= component.max_concurrent_requests
            {
                return None;
            }
            load = load.saturating_add(component.running_requests.saturating_add(local));
        }
        Some(load)
    }

    pub(crate) fn try_reserve(
        self: &Arc<Self>,
        targets: &[&AdmissionTarget],
    ) -> Option<RouteReservation> {
        let mut components = BTreeMap::new();
        for target in targets {
            for component in &target.components {
                components.insert(component.component_id.clone(), component.clone());
            }
        }
        if components.is_empty() {
            return None;
        }

        let mut reserved = self
            .reserved
            .lock()
            .expect("reservation mutex must not be poisoned");
        if components.values().any(|component| {
            component
                .running_requests
                .saturating_add(reserved.get(&component.component_id).copied().unwrap_or(0))
                >= component.max_concurrent_requests
        }) {
            return None;
        }

        let component_ids = components.keys().cloned().collect::<Vec<_>>();
        for id in &component_ids {
            *reserved.entry(id.clone()).or_default() += 1;
        }
        Some(RouteReservation {
            store: Some(self.clone()),
            component_ids,
        })
    }
}

pub struct RouteReservation {
    store: Option<Arc<ReservationStore>>,
    component_ids: Vec<BackendId>,
}

impl Drop for RouteReservation {
    fn drop(&mut self) {
        let Some(store) = self.store.take() else {
            return;
        };
        let mut reserved = store
            .reserved
            .lock()
            .expect("reservation mutex must not be poisoned");
        for id in &self.component_ids {
            let value = reserved.get_mut(id).expect("reserved component must exist");
            *value -= 1;
            if *value == 0 {
                reserved.remove(id);
            }
        }
    }
}
