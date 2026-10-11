// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Regroup ready requests and select DT workers each round, without Kubernetes discovery.

use foretoken_llm_facade::draft_target::{RoleClient, RoleStatus};
use foretoken_llm_facade::{LlmFacade, LlmFacadeResolver, RouteStage};
use foretoken_model_protocol::{
    DraftTargetCandidateFormat, GenerateInput, ModelServerRole, TokenOutput,
};
use foretoken_router::{
    ModelRouteTable, PipelineRouter, RouteDecision, RouteInventory, RouteTarget, RouteTargetId,
    RouteTargetSet, ScalingTarget, ScalingTargetKind,
};
use foretoken_server::draft_target::{
    DraftTargetRequestState, DraftTargetRound, select_draft_target_round,
};
use futures::{FutureExt, StreamExt, stream::SelectAll};
use serde::Deserialize;
use std::{
    collections::BTreeMap,
    io::{self, Read},
    sync::Arc,
};
use tokio_util::sync::CancellationToken;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Input {
    model: String,
    workers: Vec<Worker>,
    requests: Vec<GenerateInput>,
    #[serde(default)]
    migrations: Vec<Migration>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Migration {
    request_id: String,
    after_round: u64,
    target_worker: usize,
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
        self.workers[id].1.accepting
    }
    fn draft_target_context(&self, id: &RouteTargetId) -> Option<serde_json::Value> {
        self.workers[id].1.context_profile.clone()
    }
    fn draft_target_format(&self, id: &RouteTargetId) -> Option<DraftTargetCandidateFormat> {
        Some(self.workers[id].1.candidate_format)
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
    let mut migrations = input.migrations;
    let mut ready = input
        .requests
        .into_iter()
        .map(|request| DraftTargetRequestState::new(request.into(), CancellationToken::new()))
        .collect::<Result<Vec<_>, _>>()?;
    let mut executing = SelectAll::new();
    // This example's scheduler immediately dispatches currently ready members. It does not
    // wait for pending prefill, which could be queued behind a full native max_num_seqs.
    let run = async {
        loop {
            let mut groups = BTreeMap::<_, Vec<_>>::new();
            for member in ready.drain(..) {
                let requirement = DraftTargetRound::required_target(std::slice::from_ref(&member))?
                    .map(|route| (route.route_target_id, route.data_parallel_rank));
                groups.entry(requirement).or_default().push(member);
            }
            for mut members in groups.into_values() {
                eprintln!(
                    "round members: {:?}",
                    members
                        .iter()
                        .map(|member| member.request_id())
                        .collect::<Vec<_>>()
                );
                let round =
                    select_draft_target_round(&router, roles.as_ref(), &input.model, &mut members)
                        .await?
                        .ok_or("no compatible DT worker is currently available")?;
                eprintln!("worker workloads: {:?}", round.workloads());
                executing.push(round.execute());
            }
            let Some(first) = executing.next().await else {
                break;
            };
            let mut event = Some(first);
            while let Some(result) = event {
                let result = result?;
                eprintln!("round: {}", serde_json::to_string(&result.observation)?);
                if let Some(output) = result.output {
                    println!("{}", serde_json::to_string(&TokenOutput::from(output))?);
                }
                if let Some(member) = result.continuation {
                    if let Some(index) = migrations.iter().position(|plan| {
                        plan.request_id == result.request_id
                            && plan.after_round == result.observation.round
                    }) {
                        let plan = migrations.remove(index);
                        let id = RouteTargetId::new(format!("worker-{}", plan.target_worker));
                        let route = roles
                            .routes
                            .routes()
                            .iter()
                            .find(|route| route.route_target_id == id)
                            .ok_or("unknown migration Target worker")?;
                        let destination = RouteDecision {
                            route_target_id: route.route_target_id.clone(),
                            admission_targets: route.admission_targets.clone(),
                            role: route.role,
                            model: route.model.clone(),
                            revision: route.revision.clone(),
                            data_parallel_rank: 0,
                        };
                        let resolver = roles.clone();
                        executing.push(Box::pin(futures::stream::once(async move {
                            member.migrate_target(resolver.as_ref(), destination).await
                        })));
                    } else {
                        ready.push(member);
                    }
                }
                event = executing.next().now_or_never().flatten();
            }
        }
        if !migrations.is_empty() {
            return Err("migration plans were not reached before generation finished".into());
        }
        Ok::<_, Box<dyn std::error::Error>>(())
    };
    tokio::select! {
        result = tokio::signal::ctrl_c() => result?,
        result = run => result?,
    }
    Ok(())
}
