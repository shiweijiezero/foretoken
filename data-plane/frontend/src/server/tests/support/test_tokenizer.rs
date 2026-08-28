// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use foretoken_tokenizer::{Result, Tokenizer};

pub struct TestTokenizer;

impl Tokenizer for TestTokenizer {
    fn encode(&self, text: &str, _: bool) -> Result<Vec<u32>> {
        Ok(text.bytes().map(u32::from).collect())
    }

    fn encode_ordinary(&self, text: &str) -> Result<Vec<u32>> {
        self.encode(text, false)
    }

    fn decode(&self, ids: &[u32], _: bool) -> Result<String> {
        Ok(ids.iter().filter_map(|&id| char::from_u32(id)).collect())
    }

    fn token_to_id(&self, _: &str) -> Option<u32> {
        None
    }

    fn id_to_token(&self, id: u32) -> Option<String> {
        char::from_u32(id).map(|value| value.to_string())
    }
}
