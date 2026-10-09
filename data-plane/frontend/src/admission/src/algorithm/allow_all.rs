// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Unrestricted admission that retains work-unit accounting across rule updates.

use crate::{
    AdmissionCapacity, AdmissionCapacityState, AdmissionContext, AdmissionError, AdmissionPermit,
    AdmissionRequest, AdmissionRule,
};

/// Accepts requests without finite limits, retaining their count for subsequent bounded rules.
pub struct AllowAllAdmission {
    capacity: AdmissionCapacityState,
}

impl Default for AllowAllAdmission {
    fn default() -> Self {
        Self {
            capacity: AdmissionCapacityState::new(None, None),
        }
    }
}

impl AllowAllAdmission {
    /// Builds the parameter-free rule selected by the admission configuration.
    pub fn from_parameters(parameters: serde_json::Value) -> Result<Self, String> {
        if parameters
            .as_object()
            .is_some_and(|parameters| parameters.is_empty())
        {
            Ok(Self::default())
        } else {
            Err("allow_all admission accepts no parameters".into())
        }
    }
}

#[async_trait::async_trait]
impl AdmissionRule for AllowAllAdmission {
    fn capacity(&self) -> Option<AdmissionCapacity> {
        self.capacity.capacity()
    }

    fn capacity_state(&self) -> Option<&AdmissionCapacityState> {
        Some(&self.capacity)
    }

    fn requires_ready_runtime(&self) -> bool {
        self.capacity.capacity().is_some()
    }

    async fn admit(
        &self,
        request: &AdmissionRequest,
        context: &AdmissionContext<'_>,
    ) -> Result<AdmissionPermit, AdmissionError> {
        self.capacity.admit(request, context).await
    }

    fn close(&self) {
        self.capacity.close();
    }
}
