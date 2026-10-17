// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Bind concurrent requests through Router; each selected Target advances its own rounds.

use foretoken_llm_facade::draft_target::{RoleClient, RoleStatus};
use foretoken_llm_facade::{LlmFacade, LlmFacadeResolver, RouteStage};
use foretoken_model_protocol::{
    DRAFT_TARGET_PROTOCOL_VERSION, DraftTargetCapabilities, GenerateInput, ModelServerRole,
    TokenOutput,
};
use foretoken_router::{
    ModelRouteTable, PipelineRouter, RouteDecision, RouteInventory, RouteTarget, RouteTargetId,
    RouteTargetSet, Router, ScalingTarget, ScalingTargetKind,
};
use foretoken_server::draft_target::generate_draft_target_tokens;
use futures::{StreamExt, stream::SelectAll};
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    io::{self, Read},
    sync::Arc,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    model: String,
    workers: Vec<Worker>,
    requests: Vec<GenerateInput>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Worker {
    endpoint: String,
    scope: String,
}

struct Roles {
    routes: ModelRouteTable,
    workers: BTreeMap<RouteTargetId, (RoleClient, RoleStatus)>,
}
impl RouteInventory for Roles {
    fn model_routes(&self) -> &ModelRouteTable {
        &self.routes
    }
    fn is_route_target_healthy(&self, id: &RouteTargetId) -> bool {
        let status = &self.workers[id].1;
        status.accepting
            && status.candidates.is_valid_for(if status.role == "draft" {
                ModelServerRole::Draft
            } else {
                ModelServerRole::Target
            })
    }
    fn draft_pools(&self, id: &RouteTargetId) -> &[String] {
        let route = self
            .routes
            .routes()
            .iter()
            .find(|route| &route.route_target_id == id)
            .unwrap();
        if route.role == ModelServerRole::Target {
            // This diagnostic groups all same-scope workers into one compatible pool.
            std::slice::from_ref(&route.target.name)
        } else {
            &[]
        }
    }
    fn draft_target_capabilities(&self, id: &RouteTargetId) -> Option<DraftTargetCapabilities> {
        Some(self.workers[id].1.candidates.clone())
    }
}
impl LlmFacadeResolver for Roles {
    fn resolve_stage(&self, _: &RouteDecision, _: RouteStage) -> Option<Arc<dyn LlmFacade>> {
        None
    }
    fn bootstrap_endpoint(&self, _: &RouteDecision) -> Option<String> {
        None
    }
    fn resolve_draft_target(&self, decision: &RouteDecision) -> Option<RoleClient> {
        eprintln!(
            "selected {:?}: {}",
            decision.role,
            decision.route_target_id.as_str()
        );
        self.workers
            .get(&decision.route_target_id)
            .map(|(client, _)| client.clone())
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut json = String::new();
    io::stdin().read_to_string(&mut json)?;
    let input: Input = serde_json::from_str(&json)?;
    let mut workers = BTreeMap::new();
    let mut routes = Vec::new();
    let mut tokenizers = BTreeMap::new();
    for (index, worker) in input.workers.into_iter().enumerate() {
        let client = RoleClient::new(worker.endpoint)?;
        let status = client.status().await?;
        if status.protocol_version != DRAFT_TARGET_PROTOCOL_VERSION {
            return Err("update frontend and roles together to DT protocol version 4".into());
        }
        let tokenizer = (status.tokenizer.clone(), status.tokenizer_revision.clone());
        if tokenizers
            .insert(worker.scope.clone(), tokenizer.clone())
            .is_some_and(|old| old != tokenizer)
        {
            return Err("workers in a scope must use the same tokenizer identity".into());
        }
        let role = match status.role.as_str() {
            "draft" => ModelServerRole::Draft,
            "target" => ModelServerRole::Target,
            _ => return Err("expected Draft or Target role".into()),
        };
        let id = RouteTargetId::new(format!("worker-{index}"));
        let owner = ScalingTarget {
            service_uid: input.model.clone(),
            name: worker.scope.clone(),
            uid: worker.scope.clone(),
            kind: ScalingTargetKind::Pool,
        };
        routes.push(RouteTarget {
            route_target_id: id.clone(),
            target: owner.clone(),
            admission_targets: RouteTargetSet::new(vec![owner]),
            model: input.model.clone(),
            revision: String::new(),
            capabilities: Default::default(),
            max_input_tokens: Some(status.max_model_len as usize),
            ready: true,
            role,
            pipeline_scope_id: Some(worker.scope),
            data_parallel_size: 1,
        });
        workers.insert(id, (client, status));
    }
    let roles = Arc::new(Roles {
        routes: ModelRouteTable::new(routes),
        workers,
    });
    let router = PipelineRouter::new(roles.clone());
    let mut executing = SelectAll::<foretoken_llm_facade::TokenStream>::new();
    for request in input.requests {
        let request: Arc<vllm_llm::GenerateRequest> = Arc::new(request.into());
        let mut route = router
            .start(foretoken_router::RouterRequest::new(
                input.model.clone(),
                request.clone(),
            ))
            .await;
        let decision = route.select_initial()?;
        let target = roles
            .resolve_draft_target(&decision)
            .ok_or("selected Target is unavailable")?;
        let drafts = route
            .select_drafts()?
            .iter()
            .map(|draft| roles.resolve_draft_target(draft).ok_or("Draft unavailable"))
            .collect::<Result<Vec<_>, _>>()?;
        let mut stream = generate_draft_target_tokens(drafts, target, (*request).clone()).await?;
        executing.push(Box::pin(async_stream::try_stream! {
            while let Some(output) = stream.next().await {
                route.response_started();
                yield output?;
            }
            route.stage_complete();
        }));
    }
    loop {
        tokio::select! {
            result = tokio::signal::ctrl_c() => { result?; break; }
            output = executing.next() => match output {
                Some(output) => println!("{}", serde_json::to_string(&TokenOutput::from(output?))?),
                None => break,
            }
        }
    }
    Ok(())
}
