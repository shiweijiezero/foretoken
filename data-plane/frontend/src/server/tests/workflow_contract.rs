// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

mod support;

use std::collections::BTreeMap;
use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::{Json, Router, routing::get};
use foretoken_chat::ParserSelection;
use foretoken_llm_facade::{LlmFacade, LlmFacadeError, LlmFacadeResolver, RouteStage, TokenStream};
use foretoken_model_protocol::ModelServerRole;
use foretoken_router::{
    RouteDecision, RouteError, RouteSession, RouteTargetId, RouteTargetSet, Router as RouteRouter,
    RouterRequest, ScalingTarget, ScalingTargetKind,
};
use foretoken_server::{
    Generation, GenerationError, GenerationRequest, RuntimeControl, RuntimeGeneration, RuntimeState,
};
use foretoken_text::{Prompt, SamplingParams, TextDecodeOptions};
use tokio::net::TcpListener;
use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo, GenerateRequest};

use support::test_runtime;

struct WorkflowSession {
    encoder: RouteDecision,
    prefill: RouteDecision,
    decode: RouteDecision,
    stage: u8,
}

impl RouteSession for WorkflowSession {
    fn select_initial(&mut self) -> Result<RouteDecision, RouteError> {
        self.stage = 1;
        Ok(self.encoder.clone())
    }

    fn select_prefill(&mut self) -> Result<RouteDecision, RouteError> {
        if self.stage != 1 {
            return Err(RouteError::PrefillBeforeEncoder);
        }
        self.stage = 2;
        Ok(self.prefill.clone())
    }

    fn select_decode(&mut self) -> Result<RouteDecision, RouteError> {
        if self.stage != 2 {
            return Err(RouteError::DecodeBeforePrefill);
        }
        self.stage = 3;
        Ok(self.decode.clone())
    }
}

struct WorkflowRouter {
    encoder: RouteDecision,
    prefill: RouteDecision,
    decode: RouteDecision,
}

impl RouteRouter for WorkflowRouter {
    fn start(&self, _: RouterRequest) -> Box<dyn RouteSession> {
        Box::new(WorkflowSession {
            encoder: self.encoder.clone(),
            prefill: self.prefill.clone(),
            decode: self.decode.clone(),
            stage: 0,
        })
    }
}

struct StageFacade {
    stage: &'static str,
    target_id: String,
    calls: Arc<Mutex<Vec<String>>>,
    aborts: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl LlmFacade for StageFacade {
    async fn generate(&self, request: GenerateRequest) -> Result<TokenStream, LlmFacadeError> {
        assert!(
            foretoken_metrics::autoscaling_telemetry()
                .targets
                .iter()
                .any(|target| {
                    target.target.target_id == self.target_id && target.queued_requests == 1
                }),
            "{} generation must run while admitted to its scaling target",
            self.stage
        );
        self.calls
            .lock()
            .unwrap()
            .push(format!("{}:{}", self.stage, request.request_id));
        if self.stage == "decode" {
            return Err(LlmFacadeError::Unavailable);
        }
        let descriptor = (self.stage == "encoder")
            .then(|| serde_json::json!({"connector":"opaque-encoder-descriptor"}));
        Ok(Box::pin(futures::stream::iter([Ok(GenerateOutput {
            request_id: request.request_id,
            prompt_info: Some(GeneratePromptInfo {
                prompt_token_ids: vec![1].into(),
                prompt_logprobs: None,
            }),
            token_ids: vec![1],
            logprobs: None,
            finish_reason: Some(FinishReason::Length),
            cached_token_count: 0,
            kv_transfer_params: None,
            ec_transfer_params: descriptor,
        })])))
    }

    async fn abort(&self, request_ids: &[String]) -> Result<(), LlmFacadeError> {
        self.aborts.lock().unwrap().extend(
            request_ids
                .iter()
                .map(|request_id| format!("{}:{request_id}", self.stage)),
        );
        Ok(())
    }
}

struct WorkflowResolver {
    encoder: Arc<dyn LlmFacade>,
    prefill: Arc<dyn LlmFacade>,
    decode: Arc<dyn LlmFacade>,
    bootstrap: String,
}

impl LlmFacadeResolver for WorkflowResolver {
    fn resolve_stage(
        &self,
        decision: &RouteDecision,
        stage: RouteStage,
    ) -> Option<Arc<dyn LlmFacade>> {
        match (decision.route_target_id.as_str(), stage) {
            ("encoder", RouteStage::Encoder) => Some(self.encoder.clone()),
            ("prefill", RouteStage::Prefill) => Some(self.prefill.clone()),
            ("decode", RouteStage::Decode) => Some(self.decode.clone()),
            _ => None,
        }
    }

    fn bootstrap_endpoint(&self, prefill: &RouteDecision) -> Option<String> {
        (prefill.route_target_id.as_str() == "prefill").then(|| self.bootstrap.clone())
    }
}

struct ReadyControl;

#[async_trait]
impl RuntimeControl for ReadyControl {
    async fn refresh_backend_readiness(&self) {}

    fn configured_models(&self) -> Vec<String> {
        vec!["model".into()]
    }

    fn is_ready(&self) -> bool {
        true
    }
}

fn decision(
    id: &str,
    role: ModelServerRole,
    data_parallel_rank: u32,
    admission_targets: RouteTargetSet,
) -> RouteDecision {
    RouteDecision {
        route_target_id: RouteTargetId::new(id),
        admission_targets,
        role,
        model: "model".into(),
        revision: "r1".into(),
        data_parallel_rank,
    }
}

fn request() -> GenerationRequest {
    GenerationRequest {
        model: "model".into(),
        request_id: "workflow-request".into(),
        prompt: Prompt::Text("hello".into()),
        sampling_params: SamplingParams::default(),
        decode_options: TextDecodeOptions::default(),
        intermediate: false,
        priority: 0,
        cache_salt: None,
        session_id: None,
        arrival_time: None,
        tool_call_parser: ParserSelection::None,
        reasoning_parser: ParserSelection::None,
    }
}

async fn bootstrap_endpoint() -> (String, tokio::task::JoinHandle<()>) {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("http://{}", listener.local_addr().unwrap());
    let app = Router::new().route(
        "/query",
        get(|| async { Json(serde_json::json!({"0":{"engine_id":"engine-0"}})) }),
    );
    (
        endpoint,
        tokio::spawn(async move { axum::serve(listener, app).await.unwrap() }),
    )
}

#[tokio::test]
async fn runtime_workflow_aborts_every_started_stage_after_decode_admission_fails() {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let aborts = Arc::new(Mutex::new(Vec::new()));
    let (bootstrap, bootstrap_task) = bootstrap_endpoint().await;
    let facade = |stage: &'static str| -> Arc<dyn LlmFacade> {
        Arc::new(StageFacade {
            stage,
            target_id: "workflow-service".into(),
            calls: calls.clone(),
            aborts: aborts.clone(),
        })
    };
    let targets = RouteTargetSet::new(vec![ScalingTarget {
        service_uid: "workflow-service".into(),
        name: "epd".into(),
        uid: "workflow-service".into(),
        kind: ScalingTargetKind::EPDPipelineScope,
    }]);
    let encoder = decision("encoder", ModelServerRole::Encoder, 0, targets.clone());
    let prefill = decision("prefill", ModelServerRole::Prefill, 1, targets.clone());
    let decode = decision("decode", ModelServerRole::Decode, 2, targets.clone());
    let state = RuntimeState::new(
        BTreeMap::from([("model".into(), test_runtime())]),
        Arc::new(WorkflowRouter {
            encoder: encoder.clone(),
            prefill: prefill.clone(),
            decode: decode.clone(),
        }),
        Arc::new(WorkflowResolver {
            encoder: facade("encoder"),
            prefill: facade("prefill"),
            decode: facade("decode"),
            bootstrap,
        }),
    )
    .with_admission_targets("model".into(), vec![targets]);
    let generation = RuntimeGeneration::new(std::time::Duration::from_secs(1));
    assert!(generation.replace_state(1, Arc::new(state), Arc::new(ReadyControl)));

    assert!(matches!(
        generation.generate(request()).await,
        Err(GenerationError::Unavailable)
    ));
    assert_eq!(
        *calls.lock().unwrap(),
        [
            "encoder:workflow-request/encoder",
            "prefill:workflow-request/prefill",
            "decode:workflow-request/decode",
        ]
    );
    tokio::time::timeout(std::time::Duration::from_secs(1), async {
        loop {
            let aborts = aborts.lock().unwrap().clone();
            if aborts.len() == 3 {
                assert_eq!(
                    aborts,
                    [
                        "encoder:workflow-request/encoder",
                        "prefill:workflow-request/prefill",
                        "decode:workflow-request/decode",
                    ]
                );
                break;
            }
            tokio::task::yield_now().await;
        }
    })
    .await
    .unwrap();
    assert!(
        foretoken_metrics::autoscaling_telemetry()
            .targets
            .iter()
            .any(|target| {
                target.target.target_id == "workflow-service" && target.queued_requests == 0
            })
    );
    bootstrap_task.abort();
}
