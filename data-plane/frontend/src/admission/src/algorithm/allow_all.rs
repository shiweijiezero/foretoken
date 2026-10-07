// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Unrestricted admission without runtime accounting.

use crate::{AdmissionContext, AdmissionError, AdmissionPermit, AdmissionRequest, AdmissionRule};

/// Accepts requests without allocating capacity or queue state.
#[derive(Default)]
pub struct AllowAllAdmission;

impl AllowAllAdmission {
    /// Builds the parameter-free rule selected by the admission configuration.
    pub fn from_parameters(parameters: serde_json::Value) -> Result<Self, String> {
        if parameters
            .as_object()
            .is_some_and(|parameters| parameters.is_empty())
        {
            Ok(Self)
        } else {
            Err("allow_all admission accepts no parameters".into())
        }
    }
}

#[async_trait::async_trait]
impl AdmissionRule for AllowAllAdmission {
    async fn admit(
        &self,
        _request: &AdmissionRequest,
        _context: &AdmissionContext<'_>,
    ) -> Result<AdmissionPermit, AdmissionError> {
        Ok(AdmissionPermit::default())
    }
}
