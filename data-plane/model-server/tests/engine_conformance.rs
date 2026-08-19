// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Engine contract conformance suite.
//!
//! These tests pin the semantics every [`Engine`] implementation must honor.
//! The `ScriptedEngine` below is a deterministic reference implementation that
//! exercises the contract; future engine adapters (SGLang, TensorRT-LLM, ...)
//! must pass the same assertions.

use std::collections::VecDeque;
use std::sync::Mutex;
use std::sync::atomic::{AtomicUsize, Ordering};

use async_trait::async_trait;
use foretoken_model_protocol::{
    FinishReason, GenerateInput, SamplingParams, TokenEvent, TokenOutput,
};
use foretoken_model_server::engine::{
    Engine, EngineCapabilities, EngineError, EngineTelemetry, TokenStream,
};
use futures::{StreamExt, stream};

/// A deterministic engine that replays a scripted sequence of token events and
/// records every `abort`/`cleanup` call for assertions.
struct ScriptedEngine {
    events: Mutex<VecDeque<Result<TokenEvent, EngineError>>>,
    aborts: Mutex<Vec<Vec<String>>>,
    cleanups: AtomicUsize,
}

impl ScriptedEngine {
    fn new(events: Vec<Result<TokenEvent, EngineError>>) -> Self {
        Self {
            events: Mutex::new(events.into()),
            aborts: Mutex::new(Vec::new()),
            cleanups: AtomicUsize::new(0),
        }
    }

    fn aborts(&self) -> Vec<Vec<String>> {
        self.aborts.lock().unwrap().clone()
    }

    fn cleanup_count(&self) -> usize {
        self.cleanups.load(Ordering::Acquire)
    }
}

#[async_trait]
impl Engine for ScriptedEngine {
    async fn generate(&self, _request: GenerateInput) -> Result<TokenStream, EngineError> {
        let events: Vec<_> = self.events.lock().unwrap().drain(..).collect();
        Ok(Box::pin(stream::iter(events)))
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), EngineError> {
        self.aborts.lock().unwrap().push(request_ids.to_vec());
        Ok(())
    }

    fn telemetry(&self) -> EngineTelemetry {
        Default::default()
    }

    fn capabilities(&self) -> EngineCapabilities {
        Default::default()
    }

    async fn cleanup(&self) -> Result<(), EngineError> {
        self.cleanups.fetch_add(1, Ordering::AcqRel);
        Ok(())
    }
}

fn generate_input() -> GenerateInput {
    GenerateInput {
        request_id: "req".into(),
        prompt_token_ids: vec![1, 2],
        sampling_params: SamplingParams::default(),
        extensions: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        session_id: None,
    }
}

fn token(request_id: &str, token_id: u32) -> TokenEvent {
    TokenEvent::Token(Box::new(TokenOutput {
        request_id: request_id.into(),
        prompt_token_ids: None,
        prompt_logprobs: None,
        token_ids: vec![token_id],
        logprobs: None,
        cached_token_count: 0,
        finish_reason: None,
        kv_transfer_params: None,
        ec_transfer_params: None,
    }))
}

fn terminal(request_id: &str, token_id: u32) -> TokenEvent {
    TokenEvent::Token(Box::new(TokenOutput {
        request_id: request_id.into(),
        prompt_token_ids: None,
        prompt_logprobs: None,
        token_ids: vec![token_id],
        logprobs: None,
        cached_token_count: 0,
        finish_reason: Some(FinishReason::Stop(None)),
        kv_transfer_params: None,
        ec_transfer_params: None,
    }))
}

/// A valid generation stream ends with exactly one terminal item and yields
/// nothing after it.
#[tokio::test]
async fn generate_stream_ends_with_exactly_one_terminal() {
    let engine = ScriptedEngine::new(vec![
        Ok(token("req", 1)),
        Ok(token("req", 2)),
        Ok(terminal("req", 3)),
    ]);

    let mut stream = engine.generate(generate_input()).await.unwrap();
    let mut terminals = 0;
    let mut after_terminal = false;
    while let Some(event) = stream.next().await {
        let event = event.expect("scripted stream has no errors");
        if after_terminal {
            panic!("stream yielded an item after the terminal: {event:?}");
        }
        match &event {
            TokenEvent::Token(output) => {
                if output.finish_reason.is_some() {
                    terminals += 1;
                    after_terminal = true;
                }
            }
            TokenEvent::Error { .. } => {
                terminals += 1;
                after_terminal = true;
            }
        }
    }
    assert_eq!(terminals, 1, "stream must have exactly one terminal item");
    assert!(after_terminal, "stream must end with a terminal item");
}

/// An `Error` event is a valid terminal on its own.
#[tokio::test]
async fn error_event_is_a_terminal() {
    let engine = ScriptedEngine::new(vec![
        Ok(token("req", 1)),
        Ok(TokenEvent::Error {
            request_id: "req".into(),
            code: foretoken_model_protocol::TokenErrorCode::Unavailable,
        }),
    ]);

    let mut stream = engine.generate(generate_input()).await.unwrap();
    let mut terminals = 0;
    while let Some(event) = stream.next().await {
        match event {
            Ok(TokenEvent::Error { .. }) => terminals += 1,
            Ok(TokenEvent::Token(output)) => {
                assert!(output.finish_reason.is_none(), "non-terminal before error");
            }
            Err(_) => {}
        }
    }
    assert_eq!(terminals, 1);
}

/// `abort` forwards the request IDs unchanged.
#[tokio::test]
async fn abort_forwards_request_ids() {
    let engine = ScriptedEngine::new(vec![]);
    engine
        .abort(&["a".into(), "b".into()])
        .await
        .expect("abort succeeds");
    assert_eq!(
        engine.aborts(),
        vec![vec!["a".to_string(), "b".to_string()]]
    );
}

/// `cleanup` is idempotent: repeated calls succeed without error, and it
/// tolerates being called on an engine that was never started.
#[tokio::test]
async fn cleanup_is_idempotent() {
    let engine = ScriptedEngine::new(vec![]);
    engine.cleanup().await.expect("first cleanup succeeds");
    engine.cleanup().await.expect("second cleanup succeeds");
    assert_eq!(engine.cleanup_count(), 2);
}

/// The default `drain` implementation is a successful no-op.
#[tokio::test]
async fn drain_defaults_to_noop() {
    let engine = ScriptedEngine::new(vec![]);
    engine.drain().await.expect("default drain succeeds");
}

/// Default capabilities advertise nothing, so routing degrades to neutral
/// scoring.
#[test]
fn default_capabilities_advertise_nothing() {
    let capabilities = EngineCapabilities::default();
    assert_eq!(capabilities.context_length, None);
    assert_eq!(capabilities.kv_cache_block_size, None);
    assert_eq!(capabilities.total_kv_blocks, None);
    assert_eq!(capabilities.max_num_seqs, None);
    assert!(!capabilities.kv_event_sources);
    assert!(!capabilities.supports_pd);
    assert!(!capabilities.supports_ec);
}

/// `EngineError` maps to a wire-safe token error code.
#[test]
fn engine_error_token_codes() {
    use foretoken_model_protocol::TokenErrorCode;
    assert_eq!(
        EngineError::Unavailable.token_error_code(),
        TokenErrorCode::Unavailable
    );
    for error in [
        EngineError::Rejected,
        EngineError::InvalidRequest,
        EngineError::Protocol,
    ] {
        assert_eq!(error.token_error_code(), TokenErrorCode::Protocol);
    }
    assert_eq!(
        EngineError::RequestFailed.token_error_code(),
        TokenErrorCode::RequestFailed
    );
}
