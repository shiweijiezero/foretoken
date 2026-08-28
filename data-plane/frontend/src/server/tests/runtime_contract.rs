// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

mod support;
#[path = "support/test_tokenizer.rs"]
mod test_tokenizer;

use std::collections::BTreeMap;
use std::sync::Arc;

use async_trait::async_trait;
use axum::body::Body;
use axum::http::{Request, StatusCode};
use foretoken_llm_facade::{LlmFacade, LlmFacadeResolver, RouteStage};
use foretoken_router::{RouteDecision, RouteSession, Router as RouteRouter, RouterRequest};
use foretoken_server::{RuntimeControl, RuntimeGeneration, RuntimeState, router};
use tower::ServiceExt;

use support::test_runtime;

struct StaticRuntimeControl {
    ready: bool,
    configured_models: Vec<String>,
}

#[async_trait]
impl RuntimeControl for StaticRuntimeControl {
    async fn refresh_backend_readiness(&self) {}

    fn configured_models(&self) -> Vec<String> {
        self.configured_models.clone()
    }

    fn is_ready(&self) -> bool {
        self.ready
    }
}

struct UnusedRouter;

impl RouteRouter for UnusedRouter {
    fn start(&self, _: RouterRequest) -> Box<dyn RouteSession> {
        unreachable!("runtime publication does not dispatch requests")
    }
}

struct UnusedResolver;

impl LlmFacadeResolver for UnusedResolver {
    fn resolve_stage(&self, _: &RouteDecision, _: RouteStage) -> Option<Arc<dyn LlmFacade>> {
        unreachable!("runtime publication does not resolve backends")
    }

    fn bootstrap_endpoint(&self, _: &RouteDecision) -> Option<String> {
        unreachable!("runtime publication does not resolve bootstrap endpoints")
    }
}

fn published_state(model: &str) -> Arc<RuntimeState> {
    Arc::new(RuntimeState::new(
        BTreeMap::from([(model.into(), test_runtime())]),
        Arc::new(UnusedRouter),
        Arc::new(UnusedResolver),
    ))
}

fn get(uri: &str) -> Request<Body> {
    Request::builder().uri(uri).body(Body::empty()).unwrap()
}

// Protects runtime generation swaps from exposing partial state or false readiness.
#[tokio::test]
async fn runtime_publication_is_atomic_and_drives_readiness() {
    let generation = Arc::new(RuntimeGeneration::new(std::time::Duration::from_secs(1)));
    let configured_models_generation = generation.clone();
    let app = router(
        generation.clone(),
        Arc::new(move || configured_models_generation.configured_models()),
        std::time::Duration::from_secs(1),
    );

    let health = app.clone().oneshot(get("/healthz")).await.unwrap();
    let readiness = app.clone().oneshot(get("/readyz")).await.unwrap();
    assert_eq!(health.status(), StatusCode::OK);
    assert_eq!(readiness.status(), StatusCode::SERVICE_UNAVAILABLE);

    assert!(generation.replace_state(
        2,
        published_state("new"),
        Arc::new(StaticRuntimeControl {
            ready: true,
            configured_models: vec!["new".into()],
        }),
    ));
    assert!(!generation.replace_state(
        1,
        published_state("old"),
        Arc::new(StaticRuntimeControl {
            ready: false,
            configured_models: vec!["old".into()],
        }),
    ));

    let readiness = app.clone().oneshot(get("/readyz")).await.unwrap();
    assert_eq!(readiness.status(), StatusCode::OK);
    assert_eq!(generation.active_version(), Some(2));
    assert_eq!(generation.configured_models(), vec!["new"]);

    let status = app.clone().oneshot(get("/statusz")).await.unwrap();
    let status = axum::body::to_bytes(status.into_body(), usize::MAX)
        .await
        .unwrap();
    let status: serde_json::Value = serde_json::from_slice(&status).unwrap();
    assert_eq!(status["serving_ready"], true);
    assert_eq!(status["active_generation"], 2);

    generation.close_admission();
    let readiness = app.clone().oneshot(get("/readyz")).await.unwrap();
    assert_eq!(readiness.status(), StatusCode::SERVICE_UNAVAILABLE);
    let status = app.clone().oneshot(get("/statusz")).await.unwrap();
    let status = axum::body::to_bytes(status.into_body(), usize::MAX)
        .await
        .unwrap();
    let status: serde_json::Value = serde_json::from_slice(&status).unwrap();
    assert_eq!(status["serving_ready"], false);
    assert_eq!(status["active_generation"], 2);

    assert!(generation.replace_state(
        3,
        published_state("next"),
        Arc::new(StaticRuntimeControl {
            ready: false,
            configured_models: vec!["next".into()],
        }),
    ));
    let readiness = app.oneshot(get("/readyz")).await.unwrap();
    assert_eq!(readiness.status(), StatusCode::SERVICE_UNAVAILABLE);
    assert_eq!(generation.active_version(), Some(3));
    assert_eq!(generation.configured_models(), vec!["next"]);
}
