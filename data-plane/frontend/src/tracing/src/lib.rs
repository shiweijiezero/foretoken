// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Foretoken's compatibility boundary for tracing initialization and W3C request context.

use std::collections::BTreeMap;

pub use vllm_tracing::*;

/// W3C trace context headers carried through the frontend and model-server protocol.
pub const TRACEPARENT_HEADER: &str = "traceparent";
pub const TRACESTATE_HEADER: &str = "tracestate";
/// Stable response and log correlation header emitted by Foretoken HTTP servers.
pub const REQUEST_ID_HEADER: &str = "x-request-id";

/// Request context owned by the HTTP boundary and copied into every internal generation request.
#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub struct RequestContext {
    pub request_id: String,
    pub trace_headers: Option<BTreeMap<String, String>>,
}

impl RequestContext {
    /// Returns the W3C parent value for span fields and transport propagation.
    pub fn traceparent(&self) -> Option<&str> {
        self.trace_headers
            .as_ref()
            .and_then(|headers| headers.get(TRACEPARENT_HEADER))
            .map(String::as_str)
    }

    /// Returns the optional W3C state value for span fields and transport propagation.
    pub fn tracestate(&self) -> Option<&str> {
        self.trace_headers
            .as_ref()
            .and_then(|headers| headers.get(TRACESTATE_HEADER))
            .map(String::as_str)
    }
}

/// Keeps only W3C propagation headers that are safe to copy into an internal request.
pub fn w3c_trace_headers(
    traceparent: Option<&str>,
    tracestate: Option<&str>,
) -> Option<BTreeMap<String, String>> {
    let mut headers = BTreeMap::new();
    if let Some(value) = traceparent.filter(|value| !value.trim().is_empty()) {
        headers.insert(TRACEPARENT_HEADER.to_owned(), value.to_owned());
    }
    if let Some(value) = tracestate.filter(|value| !value.trim().is_empty()) {
        headers.insert(TRACESTATE_HEADER.to_owned(), value.to_owned());
    }
    (!headers.is_empty()).then_some(headers)
}
