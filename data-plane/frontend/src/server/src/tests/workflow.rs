// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

use std::sync::{Arc, Mutex};

use async_trait::async_trait;
use axum::{Json, Router, routing::get};
use foretoken_llm_facade::{LlmFacade, LlmFacadeError, LlmFacadeResolver, TokenStream};
use foretoken_model_protocol::{ModelServerRole, RouteStage};
use foretoken_router::{
    RouteDecision, RouteError, RouteSession, RouteTargetId, RouteTargetSet, ScalingTarget,
    ScalingTargetKind,
};
use tokio::net::TcpListener;
use vllm_llm::{FinishReason, GenerateOutput, GeneratePromptInfo, GenerateRequest};

use crate::GenerationError;
use crate::runtime::workflow::execute_workflow;

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

struct StageFacade {
    stage: &'static str,
    target_id: String,
    calls: Arc<Mutex<Vec<String>>>,
    aborts: Arc<Mutex<Vec<String>>>,
}

#[async_trait]
impl LlmFacade for StageFacade {
    async fn generate(&self, request: GenerateRequest) -> Result<TokenStream, LlmFacadeError> {
        let queued = foretoken_metrics::autoscaling_telemetry()
            .targets
            .iter()
            .any(|value| value.target.target_id == self.target_id && value.queued_requests == 1);
        assert!(
            queued,
            "{} generate must run while its target is queued",
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

fn decision(id: &str, role: ModelServerRole, data_parallel_rank: u32) -> RouteDecision {
    RouteDecision {
        route_target_id: RouteTargetId::new(id),
        role,
        model: "model".into(),
        revision: "r1".into(),
        data_parallel_rank,
    }
}

fn request() -> GenerateRequest {
    GenerateRequest {
        request_id: "workflow-request".into(),
        prompt_token_ids: vec![1],
        sampling_params: Default::default(),
        mm_features: None,
        arrival_time: None,
        cache_salt: None,
        trace_headers: None,
        priority: 0,
        data_parallel_rank: None,
        reasoning_parser_kwargs: None,
        lora_request: None,
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
async fn workflow_orders_epd_tracks_admission_and_aborts_every_child_on_decode_failure() {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let aborts = Arc::new(Mutex::new(Vec::new()));
    let (bootstrap, bootstrap_task) = bootstrap_endpoint().await;
    let make_facade = |stage: &'static str, target_id: &str| -> Arc<dyn LlmFacade> {
        Arc::new(StageFacade {
            stage,
            target_id: target_id.into(),
            calls: calls.clone(),
            aborts: aborts.clone(),
        })
    };
    let resolver = WorkflowResolver {
        encoder: make_facade("encoder", "workflow-service"),
        prefill: make_facade("prefill", "workflow-service"),
        decode: make_facade("decode", "workflow-service"),
        bootstrap,
    };
    let targets = RouteTargetSet::new(vec![ScalingTarget {
        service_uid: "workflow-service".into(),
        name: "epd".into(),
        uid: "workflow-service".into(),
        kind: ScalingTargetKind::EPDDomain,
    }]);
    foretoken_metrics::register_targets(&targets);
    let _queue = foretoken_metrics::QueueGuard::new(&targets);
    let mut session = WorkflowSession {
        encoder: decision("encoder", ModelServerRole::Encoder, 0),
        prefill: decision("prefill", ModelServerRole::Prefill, 1),
        decode: decision("decode", ModelServerRole::Decode, 2),
        stage: 0,
    };

    assert!(matches!(
        execute_workflow(&resolver, &mut session, request()).await,
        Err(GenerationError::Unavailable)
    ));
    drop(_queue);
    assert_eq!(
        *calls.lock().unwrap(),
        [
            "encoder:workflow-request/encoder",
            "prefill:workflow-request/prefill",
            "decode:workflow-request/decode",
        ]
    );
    assert!(
        foretoken_metrics::autoscaling_telemetry()
            .targets
            .iter()
            .any(|value| {
                value.target.target_id == "workflow-service" && value.queued_requests == 0
            })
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
    bootstrap_task.abort();
}
