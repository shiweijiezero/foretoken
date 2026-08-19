// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine-neutral wire types shared by the frontend and model-server.
//!
//! These types are the Foretoken-owned subset of the model-server protocol.
//! They intentionally do not depend on any inference-engine crate; engine
//! adapters (vLLM, SGLang, ...) translate between these neutral types and
//! their engine-native representations at the request and response edges.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};

/// Effective model dtype reported by the engine after config resolution.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum ModelDtype {
    #[serde(rename = "float16")]
    Float16,
    #[serde(rename = "bfloat16")]
    BFloat16,
    #[serde(rename = "float32")]
    Float32,
}

impl ModelDtype {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Float16 => "float16",
            Self::BFloat16 => "bfloat16",
            Self::Float32 => "float32",
        }
    }
}

/// The stop reason associated with a finished output.
///
/// Modeled as a tagged enum over an integer token id or an explicit stop
/// string.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum StopReason {
    TokenId(u32),
    Text(String),
}

/// The reason a request finished.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum FinishReason {
    /// Generation stopped for a stop string, stop token, or EOS.
    ///
    /// The inner stop reason is present for explicit stop strings or stop
    /// tokens, and absent for EOS-driven stops.
    Stop(Option<StopReason>),
    /// `max_tokens` or `max_model_len` was reached.
    Length,
    /// The request was aborted by the client.
    Abort,
    /// A retryable request-level internal error occurred.
    Error,
    /// A repetitive token pattern was detected.
    Repetition(Option<StopReason>),
}

impl FinishReason {
    /// Returns a human-readable string for this finish reason, used for metrics
    /// and reporting.
    pub fn as_str(&self) -> &'static str {
        match self {
            Self::Stop(_) => "stop",
            Self::Length => "length",
            Self::Abort => "abort",
            Self::Error => "error",
            Self::Repetition(_) => "repetition",
        }
    }
}

/// One token candidate and its logprob metadata for a single sequence position.
///
/// The first entry in a [`PositionLogprobs`] is always the sampled/selected
/// token for that position. Remaining entries follow the engine's returned
/// top-k candidate order.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct TokenLogprob {
    pub token_id: u32,
    pub logprob: f32,
    /// The sampled/selected token uses its actual vocab rank. Remaining entries
    /// use 1-based top-k ranks matching the engine's returned candidate order.
    pub rank: u32,
}

/// Logprob payload for one sequence position.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct PositionLogprobs {
    pub entries: Vec<TokenLogprob>,
}

/// Decoded per-request logprobs payload: one [`PositionLogprobs`] per scored
/// position, each containing the sampled/selected token plus any returned
/// top-k alternatives for that same position.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Logprobs {
    pub positions: Vec<PositionLogprobs>,
}

impl Logprobs {
    /// Returns the number of scored positions in this payload.
    pub fn len(&self) -> usize {
        self.positions.len()
    }

    /// Returns whether the payload contains no scored positions.
    pub fn is_empty(&self) -> bool {
        self.positions.is_empty()
    }
}

fn default_temperature() -> f32 {
    1.0
}

fn default_top_p() -> f32 {
    1.0
}

fn default_repetition_penalty() -> f32 {
    1.0
}

fn default_max_tokens() -> u32 {
    16
}

/// Engine-neutral sampling parameters for text generation.
///
/// This is the common subset shared across inference engines. Engine-specific
/// sampling knobs are carried through [`SamplingParams::extra_args`] rather
/// than modeled as typed fields here.
#[serde_with::skip_serializing_none]
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct SamplingParams {
    /// Controls randomness. Lower values are more deterministic; zero means
    /// greedy sampling.
    #[serde(default = "default_temperature")]
    pub temperature: f32,
    /// Cumulative probability threshold for nucleus sampling.
    #[serde(default = "default_top_p")]
    pub top_p: f32,
    /// Maximum number of top tokens to consider. `0` means all tokens.
    pub top_k: u32,
    /// Random seed used by the sampler when present.
    pub seed: Option<i64>,
    /// Maximum number of tokens to generate per output sequence.
    #[serde(default = "default_max_tokens")]
    pub max_tokens: u32,
    /// Minimum number of tokens to generate before EOS or stop-token handling.
    pub min_tokens: u32,
    /// Minimum probability threshold for token sampling.
    pub min_p: f32,
    /// Frequency penalty applied by the sampler.
    pub frequency_penalty: f32,
    /// Presence penalty applied by the sampler.
    pub presence_penalty: f32,
    /// Repetition penalty applied by the sampler.
    #[serde(default = "default_repetition_penalty")]
    pub repetition_penalty: f32,
    /// Number of log probabilities to return per generated token.
    ///
    /// `None` disables sample logprobs. `-1` requests the full vocabulary.
    pub logprobs: Option<i32>,
    /// Number of log probabilities to return per prompt token.
    ///
    /// `None` disables prompt logprobs. `-1` requests the full vocabulary.
    pub prompt_logprobs: Option<i32>,
    /// Token IDs that stop generation.
    pub stop_token_ids: Vec<u32>,
    /// Engine-specific sampling parameters not represented by the typed fields.
    pub extra_args: BTreeMap<String, serde_json::Value>,
}

impl Default for SamplingParams {
    fn default() -> Self {
        Self {
            temperature: default_temperature(),
            top_p: default_top_p(),
            top_k: 0,
            seed: None,
            max_tokens: default_max_tokens(),
            min_tokens: 0,
            min_p: 0.0,
            frequency_penalty: 0.0,
            presence_penalty: 0.0,
            repetition_penalty: default_repetition_penalty(),
            logprobs: None,
            prompt_logprobs: None,
            stop_token_ids: Vec::new(),
            extra_args: BTreeMap::new(),
        }
    }
}

/// Engine-specific request extensions carried opaquely through the neutral
/// protocol. Engine adapters serialize and deserialize each field using their
/// native representation.
///
/// Values are messagepack-typed (`rmpv::Value`) rather than JSON so binary
/// extension data (for example multimodal tensors encoded as messagepack
/// extension types) round-trips losslessly through the wire.
#[derive(Debug, Clone, Default, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct EngineExtensions {
    /// Optional multimodal features already prepared by the frontend.
    pub mm_features: Option<rmpv::Value>,
    /// Optional LoRA adapter request applied to this generation.
    pub lora_request: Option<rmpv::Value>,
    /// Optional reasoning-parser kwargs forwarded to engine-side structured
    /// output logic.
    pub reasoning_parser_kwargs: Option<rmpv::Value>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn model_dtype_serde_round_trips() {
        assert_eq!(
            serde_json::to_value(ModelDtype::Float16).unwrap(),
            serde_json::json!("float16")
        );
        assert_eq!(
            serde_json::from_value::<ModelDtype>(serde_json::json!("bfloat16")).unwrap(),
            ModelDtype::BFloat16
        );
        assert_eq!(ModelDtype::Float32.as_str(), "float32");
    }

    #[test]
    fn finish_reason_serde_uses_external_tagging() {
        assert_eq!(
            serde_json::to_value(FinishReason::Length).unwrap(),
            serde_json::json!("Length")
        );
        assert_eq!(
            serde_json::to_value(FinishReason::Stop(None)).unwrap(),
            serde_json::json!({"Stop": null})
        );
        assert_eq!(
            serde_json::to_value(FinishReason::Stop(Some(StopReason::TokenId(5)))).unwrap(),
            serde_json::json!({"Stop": 5})
        );
        assert_eq!(
            serde_json::to_value(FinishReason::Stop(Some(StopReason::Text("</s>".into()))))
                .unwrap(),
            serde_json::json!({"Stop": "</s>"})
        );
    }

    #[test]
    fn finish_reason_as_str() {
        assert_eq!(FinishReason::Stop(None).as_str(), "stop");
        assert_eq!(FinishReason::Length.as_str(), "length");
        assert_eq!(FinishReason::Abort.as_str(), "abort");
        assert_eq!(FinishReason::Error.as_str(), "error");
        assert_eq!(FinishReason::Repetition(None).as_str(), "repetition");
    }

    #[test]
    fn logprobs_round_trips() {
        let logprobs = Logprobs {
            positions: vec![PositionLogprobs {
                entries: vec![TokenLogprob {
                    token_id: 1,
                    logprob: -0.5,
                    rank: 1,
                }],
            }],
        };
        let value = serde_json::to_value(&logprobs).unwrap();
        assert_eq!(serde_json::from_value::<Logprobs>(value).unwrap(), logprobs);
        assert_eq!(logprobs.len(), 1);
        assert!(!logprobs.is_empty());
        assert!(Logprobs { positions: vec![] }.is_empty());
    }

    #[test]
    fn sampling_params_defaults_match_expected() {
        let params = SamplingParams::default();
        assert_eq!(params.temperature, 1.0);
        assert_eq!(params.top_p, 1.0);
        assert_eq!(params.top_k, 0);
        assert_eq!(params.seed, None);
        assert_eq!(params.max_tokens, 16);
        assert_eq!(params.min_tokens, 0);
        assert_eq!(params.min_p, 0.0);
        assert_eq!(params.frequency_penalty, 0.0);
        assert_eq!(params.presence_penalty, 0.0);
        assert_eq!(params.repetition_penalty, 1.0);
        assert_eq!(params.logprobs, None);
        assert_eq!(params.prompt_logprobs, None);
        assert!(params.stop_token_ids.is_empty());
        assert!(params.extra_args.is_empty());
    }

    #[test]
    fn sampling_params_decodes_omitted_defaults() {
        let decoded: SamplingParams =
            serde_json::from_str(r#"{"stop_token_ids":[151643]}"#).unwrap();
        assert_eq!(decoded.stop_token_ids, vec![151643]);
        assert_eq!(decoded.temperature, 1.0);
        assert_eq!(decoded.top_p, 1.0);
        assert_eq!(decoded.top_k, 0);
        assert_eq!(decoded.max_tokens, 16);
        assert_eq!(decoded.repetition_penalty, 1.0);
        assert_eq!(decoded.logprobs, None);
    }

    #[test]
    fn sampling_params_skips_none_fields_on_serialize() {
        let params = SamplingParams {
            seed: None,
            logprobs: Some(2),
            ..Default::default()
        };
        let value = serde_json::to_value(&params).unwrap();
        assert!(value.get("seed").is_none());
        assert_eq!(value["logprobs"], 2);
    }

    #[test]
    fn engine_extensions_default_all_none() {
        let extensions = EngineExtensions::default();
        assert!(extensions.mm_features.is_none());
        assert!(extensions.lora_request.is_none());
        assert!(extensions.reasoning_parser_kwargs.is_none());
    }

    #[test]
    fn engine_extensions_round_trips_opaque_values() {
        let extensions = EngineExtensions {
            mm_features: Some(rmpv::Value::String("image".into())),
            lora_request: None,
            reasoning_parser_kwargs: None,
        };
        let value = serde_json::to_value(&extensions).unwrap();
        assert_eq!(
            serde_json::from_value::<EngineExtensions>(value).unwrap(),
            extensions
        );
    }
}
