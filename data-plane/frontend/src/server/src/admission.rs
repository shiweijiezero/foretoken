// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Captures caller identity and candidate weight before input preparation.

use std::time::Instant;

use foretoken_admission::{AdmissionRequest, CallerIdentity};

use crate::runtime::GenerationRequest;

/// Original HTTP timing retained by CPU-only endpoints through protocol conversion.
#[derive(Clone, Copy, Debug)]
pub struct AdmissionOrigin {
    pub received_at: Instant,
}

/// Captures one generation candidate without inspecting or duplicating its input payload.
pub(crate) fn generation(request: &GenerationRequest) -> AdmissionRequest {
    AdmissionRequest {
        model: request.model.clone(),
        caller: CallerIdentity::current(),
        units: 1,
        received_at: request.started_at,
    }
}

/// Gives tokenization and detokenization one preparation unit and no backend concurrency.
pub(crate) fn preprocessing(model: &str, origin: AdmissionOrigin) -> AdmissionRequest {
    AdmissionRequest {
        model: model.to_owned(),
        caller: CallerIdentity::current(),
        units: 1,
        received_at: origin.received_at,
    }
}
