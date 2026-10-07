// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Immutable request facts normalized before admission, without preprocessing payloads.

use std::time::Instant;

/// Work requested from the frontend, independent of its external API dialect.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionOperation {
    Completion,
    Chat,
    Tokenization,
    Detokenization,
}

/// Client API that produced a normalized request; internal callers may omit it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionApi {
    OpenAi,
    Responses,
    Messages,
}

/// Representation received by the frontend, before chat rendering or tokenization.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum AdmissionInputKind {
    Text,
    TokenIds,
    Chat,
}

/// Known token count or an explicitly identified estimate; unknown is not zero.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum AdmissionTokenCount {
    #[default]
    Unknown,
    Estimated(u64),
    Exact(u64),
}

/// Media items actually present in an input, not capabilities advertised by its model.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub struct AdmissionMedia {
    pub images: usize,
    pub audio: usize,
    pub video: usize,
}

/// Size summary of one prompt or chat conversation; excludes encoded media and template expansion.
#[derive(Clone, Debug)]
pub struct AdmissionInput {
    pub kind: AdmissionInputKind,
    /// UTF-8 bytes of supplied textual content; absent for token-ID-only inputs.
    pub text_bytes: Option<usize>,
    /// Complete input-token cost, including media expansion when known.
    pub tokens: AdmissionTokenCount,
    pub messages: Option<usize>,
    pub media: AdmissionMedia,
}

/// Per-candidate output bounds and cost estimate. Tokenization has no output-generation budget.
#[derive(Clone, Copy, Debug, Default)]
pub struct AdmissionOutput {
    /// User-requested stopping limit, before protocol-specific lowering.
    pub requested_max_tokens: Option<u32>,
    /// Limit already selected for execution; absent while model defaults remain unresolved.
    pub execution_max_tokens: Option<u32>,
    /// Predicted output work, not a stopping limit; absent without an estimator.
    pub expected_tokens: Option<u32>,
}

/// Common facts supplied to every admission rule, without retaining a model runtime or request body.
#[derive(Clone, Debug)]
pub struct AdmissionRequest {
    pub model: String,
    pub operation: AdmissionOperation,
    pub api: Option<AdmissionApi>,
    pub request_id: Option<String>,
    /// One summary per distinct input; candidates reuse these inputs without duplicating them.
    pub inputs: Vec<AdmissionInput>,
    /// Includes candidates generated for best-of selection, not only returned results.
    pub candidates_per_input: u32,
    pub output: AdmissionOutput,
    /// Client scheduling preference, not an authenticated service priority.
    pub requested_priority: i32,
    pub stream: bool,
    /// Monotonic frontend processing origin shared by all candidates in this request.
    pub received_at: Instant,
}

impl AdmissionRequest {
    /// Returns the complete candidate weight, or None when it cannot fit the admission counter.
    pub fn units(&self) -> Option<u32> {
        u32::try_from(self.inputs.len())
            .ok()?
            .checked_mul(self.candidates_per_input)
    }
}
