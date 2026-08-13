// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Mock token stream used until the real backend path lands.
//!
//! TODO: remove once the chat facade (render + tokenize) and model-protocol
//! (`GenerateInput`) are wired in (Step 1 of the design proposal).

use futures::stream::{self, BoxStream, StreamExt};

/// Fixed token sequence returned for every request, regardless of input.
pub const MOCK_TOKENS: &[&str] = &["Hello", " world", "!"];

/// Returns a stream of mock tokens.
pub fn mock_token_stream() -> BoxStream<'static, String> {
    stream::iter(MOCK_TOKENS.iter().map(|s| s.to_string())).boxed()
}
