// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Snapshot projection into routing and component inventory.

use foretoken_kv_indexer::{KvEventSourceConfig, KvRouteBinding, KvRuntimeConfig};
use foretoken_llm_facade::HttpFacade;
use foretoken_model_protocol::{
    KvCacheLocality, KvConnector, KvPlacement, KvStorageTier, ModelServerRole,
};
use foretoken_router::{
    ModelRouteTable, RouteTarget, RouteTargetId, RouteTargetSet, ScalingTarget, ScalingTargetKind,
};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use crate::registry::Component;
use crate::snapshot::{
    ServingSnapshot, SnapshotEpdComponent, SnapshotEpdPipelineScope, SnapshotError, SnapshotGroup,
    SnapshotPdComponent, SnapshotPdPipelineScope,
};

/// Projects validated snapshot topology into the router's route table and backend components.
///
/// `BackendRegistry::from_snapshot` takes ownership of both outputs; this function consumes the snapshot.
pub(crate) fn project_registry(
    snapshot: ServingSnapshot,
) -> Result<(ModelRouteTable, BTreeMap<RouteTargetId, Component>), SnapshotError> {
    // Validate snapshot-wide identity and admission ownership before consuming topology
    // vectors so every emitted route receives one unambiguous scaling target set.
    if snapshot.version == 0 {
        return Err(SnapshotError::InvalidVersion);
    }
    snapshot.model_identities()?;
    let mut admission_by_target = BTreeMap::<ScalingTarget, RouteTargetSet>::new();
    for sets in snapshot.admission_target_sets()?.into_values() {
        for set in sets {
            for target in set.targets() {
                if admission_by_target
                    .insert(target.clone(), set.clone())
                    .is_some_and(|existing| existing != set)
                {
                    return Err(SnapshotError::ConflictingAdmissionTarget(
                        target.uid.clone(),
                    ));
                }
            }
        }
    }
    let mut routes = Vec::new();
    let mut components = BTreeMap::new();
    let aggregate_models = project_aggregate(
        snapshot.groups,
        &admission_by_target,
        &mut routes,
        &mut components,
    )?;
    let pd_models = project_pd(
        snapshot.pd_components,
        &snapshot.pd_pipeline_scopes,
        &aggregate_models,
        &admission_by_target,
        &mut routes,
        &mut components,
    )?;
    project_epd(
        snapshot.epd_components,
        &snapshot.epd_pipeline_scopes,
        &aggregate_models,
        &pd_models,
        &admission_by_target,
        &mut routes,
        &mut components,
    )?;
    Ok((ModelRouteTable::new(routes), components))
}

/// Projects KV event sources and route bindings from a serving snapshot.
///
/// `BackendRegistryBuild` consumes this derived runtime configuration while the snapshot remains controller-owned.
pub(crate) fn project_kv_runtime(
    snapshot: &ServingSnapshot,
) -> Result<KvRuntimeConfig, SnapshotError> {
    let mut sources = BTreeMap::new();
    let mut bindings = BTreeMap::new();

    let mut add_route = |route_target_id: &RouteTargetId,
                         endpoint: &str,
                         model_revision: &str,
                         scope_id: &str,
                         store_id: Option<&str>,
                         data_parallel_size: u32| {
        let mut rank_sources = BTreeMap::new();
        for dp_rank in 0..data_parallel_size {
            let event_source_id = format!("{}:dp:{dp_rank}", route_target_id.as_str());
            sources.insert(
                event_source_id.clone(),
                KvEventSourceConfig {
                    event_source_id: event_source_id.clone(),
                    model_group_id: route_target_id.as_str().to_owned(),
                    endpoint: endpoint.to_owned(),
                    dp_rank,
                    model_revision: model_revision.to_owned(),
                    scope_id: scope_id.to_owned(),
                },
            );
            rank_sources.insert(dp_rank, event_source_id);
        }
        let shared_placement = store_id.map(|_| KvPlacement {
            tier: KvStorageTier::External,
            locality: KvCacheLocality::Remote,
        });
        let mut readable_placements: BTreeSet<_> = [KvPlacement {
            tier: KvStorageTier::Device,
            locality: KvCacheLocality::Local,
        }]
        .into_iter()
        .collect();
        readable_placements.extend(shared_placement);
        bindings.insert(
            route_target_id.as_str().to_owned(),
            KvRouteBinding {
                data_parallel_rank_event_source_ids: rank_sources,
                readable_placements,
                can_restore_or_transfer: store_id.is_some(),
                shared_lookup_scope: store_id.map(str::to_owned),
                shared_lookup_placement: shared_placement,
            },
        );
    };
    for group in &snapshot.groups {
        if group.capabilities.contains("video") {
            continue;
        }
        add_route(
            &group.route_target_id,
            &group.endpoint,
            &group.revision,
            &group.kv_scope_id,
            group.kv_lookup_scope.as_deref(),
            group.data_parallel_size,
        );
    }
    for component in &snapshot.pd_components {
        if matches!(
            component.role,
            ModelServerRole::Prefill | ModelServerRole::Decode
        ) {
            add_route(
                &component.route_target_id,
                &component.endpoint,
                &component.revision,
                &component.kv_scope_id,
                component.kv_lookup_scope.as_deref(),
                component.data_parallel_size,
            );
        }
    }
    for component in &snapshot.epd_components {
        if component.role == ModelServerRole::Prefill {
            add_route(
                &component.route_target_id,
                &component.endpoint,
                &component.revision,
                &component.kv_scope_id,
                component.kv_lookup_scope.as_deref(),
                component.data_parallel_size,
            );
        }
    }
    Ok(KvRuntimeConfig {
        event_sources: sources.into_values().collect(),
        route_bindings: bindings,
        requested_implementation: Default::default(),
    })
}

/// Adds independently executable groups while retaining one facade per physical endpoint.
fn project_aggregate(
    groups: Vec<SnapshotGroup>,
    admission_by_target: &BTreeMap<ScalingTarget, RouteTargetSet>,
    routes: &mut Vec<RouteTarget>,
    components: &mut BTreeMap<RouteTargetId, Component>,
) -> Result<BTreeSet<String>, SnapshotError> {
    let mut aggregate_models = BTreeSet::new();
    for group in groups {
        if group.service_uid.is_empty()
            || group.pool_uid.is_empty()
            || group.pool_name.is_empty()
            || group.route_target_id.as_str().is_empty()
            || group.endpoint.is_empty()
            || group.kv_scope_id.is_empty()
        {
            return Err(SnapshotError::IncompleteGroup(group.route_target_id));
        }
        let facade = Arc::new(HttpFacade::new(group.endpoint.clone()).map_err(|error| {
            SnapshotError::InvalidEndpoint {
                endpoint: group.endpoint.clone(),
                message: error.to_string(),
            }
        })?);
        if components
            .insert(
                group.route_target_id.clone(),
                Component::Aggregate {
                    endpoint: group.endpoint,
                    facade,
                },
            )
            .is_some()
        {
            return Err(SnapshotError::DuplicateRouteTarget(group.route_target_id));
        }
        aggregate_models.insert(group.model.clone());
        let target = pool_target(group.service_uid, group.pool_uid, group.pool_name);
        routes.push(RouteTarget {
            route_target_id: group.route_target_id,
            admission_targets: admission_targets(admission_by_target, &target)?,
            target,
            model: group.model,
            revision: group.revision,
            capabilities: group.capabilities,
            max_input_tokens: group.max_input_tokens,
            ready: true,
            role: ModelServerRole::Aggregate,
            pipeline_scope_id: None,
            data_parallel_size: group.data_parallel_size,
        });
    }
    Ok(aggregate_models)
}

/// Adds P/D routes only within the snapshot's explicit prefill/decode scope membership.
fn project_pd(
    pd_components: Vec<SnapshotPdComponent>,
    pd_pipeline_scopes: &[SnapshotPdPipelineScope],
    aggregate_models: &BTreeSet<String>,
    admission_by_target: &BTreeMap<ScalingTarget, RouteTargetSet>,
    routes: &mut Vec<RouteTarget>,
    components: &mut BTreeMap<RouteTargetId, Component>,
) -> Result<BTreeSet<String>, SnapshotError> {
    let mut pd_models = BTreeSet::new();
    // Components are admitted independently by Pool, but routing requires explicit membership.
    let mut pipeline_scope_members =
        BTreeMap::<String, (BTreeSet<RouteTargetId>, BTreeSet<RouteTargetId>)>::new();
    for pipeline_scope in pd_pipeline_scopes {
        pipeline_scope_members.insert(
            pipeline_scope.pipeline_scope_id.clone(),
            (
                pipeline_scope
                    .prefill_route_target_ids
                    .iter()
                    .cloned()
                    .collect(),
                pipeline_scope
                    .decode_route_target_ids
                    .iter()
                    .cloned()
                    .collect(),
            ),
        );
    }
    for component in pd_components {
        if component.service_uid.is_empty()
            || component.pool_uid.is_empty()
            || component.pool_name.is_empty()
            || component.route_target_id.as_str().is_empty()
            || component.pipeline_scope_id.is_empty()
            || component.endpoint.is_empty()
            || component.kv_scope_id.is_empty()
        {
            return Err(SnapshotError::IncompletePdComponent(
                component.route_target_id,
            ));
        }
        let connector = component.connector;
        if !matches!(component.protocol.as_str(), "rdma" | "tcp") {
            return Err(SnapshotError::UnsupportedPdTransport(
                component.route_target_id,
            ));
        }
        if component.role == ModelServerRole::Aggregate
            || aggregate_models.contains(&component.model)
        {
            return Err(SnapshotError::MixedModelRoles(component.model));
        }
        pd_models.insert(component.model.clone());
        if component.role == ModelServerRole::Prefill
            && connector == KvConnector::Mooncake
            && component.prefill_bootstrap_endpoint.is_none()
        {
            return Err(SnapshotError::IncompletePdComponent(
                component.route_target_id,
            ));
        }
        let member = pipeline_scope_members
            .get(&component.pipeline_scope_id)
            .ok_or_else(|| {
                SnapshotError::InvalidPdPipelineScope(component.pipeline_scope_id.clone())
            })?;
        if !(if component.role == ModelServerRole::Prefill {
            member.0.contains(&component.route_target_id)
        } else {
            member.1.contains(&component.route_target_id)
        }) {
            return Err(SnapshotError::InvalidPdPipelineScope(
                component.pipeline_scope_id,
            ));
        }
        let target = pool_target(
            component.service_uid.clone(),
            component.pool_uid.clone(),
            component.pool_name.clone(),
        );
        let route = RouteTarget {
            route_target_id: component.route_target_id.clone(),
            admission_targets: admission_targets(admission_by_target, &target)?,
            target,
            model: component.model,
            revision: component.revision,
            capabilities: component.capabilities,
            max_input_tokens: component.max_input_tokens,
            ready: true,
            role: component.role,
            pipeline_scope_id: Some(component.pipeline_scope_id),
            data_parallel_size: component.data_parallel_size,
        };
        let component = match route.role {
            ModelServerRole::Prefill => Component::Prefill {
                endpoint: component.endpoint,
                bootstrap: component.prefill_bootstrap_endpoint,
                connector,
            },
            ModelServerRole::Decode => Component::Decode {
                endpoint: component.endpoint,
                connector,
            },
            ModelServerRole::Aggregate | ModelServerRole::Encoder => {
                return Err(SnapshotError::InvalidPdPipelineScope(
                    route.pipeline_scope_id.clone().unwrap_or_default(),
                ));
            }
        };
        if components
            .insert(route.route_target_id.clone(), component)
            .is_some()
        {
            return Err(SnapshotError::DuplicateRouteTarget(route.route_target_id));
        }
        routes.push(route);
    }
    for (pipeline_scope_id, (prefill_ids, decode_ids)) in pipeline_scope_members {
        if prefill_ids.is_empty() || decode_ids.is_empty() {
            return Err(SnapshotError::InvalidPdPipelineScope(pipeline_scope_id));
        }
    }
    Ok(pd_models)
}

/// Adds E/P/D routes after verifying complete, unique, service-local scope membership.
fn project_epd(
    epd_components: Vec<SnapshotEpdComponent>,
    epd_pipeline_scopes: &[SnapshotEpdPipelineScope],
    aggregate_models: &BTreeSet<String>,
    pd_models: &BTreeSet<String>,
    admission_by_target: &BTreeMap<ScalingTarget, RouteTargetSet>,
    routes: &mut Vec<RouteTarget>,
    components: &mut BTreeMap<RouteTargetId, Component>,
) -> Result<(), SnapshotError> {
    // The controller owns connector compatibility. Projection verifies structural scope ownership.
    let mut epd_components_by_id = BTreeMap::new();
    for component in &epd_components {
        if component.service_uid.is_empty()
            || component.pool_uid.is_empty()
            || component.pool_name.is_empty()
            || component.route_target_id.as_str().is_empty()
            || component.endpoint.is_empty()
            || component.kv_scope_id.is_empty()
        {
            return Err(SnapshotError::IncompleteEpdComponent(
                component.route_target_id.clone(),
            ));
        }
        if epd_components_by_id
            .insert(component.route_target_id.clone(), component)
            .is_some()
        {
            return Err(SnapshotError::DuplicateRouteTarget(
                component.route_target_id.clone(),
            ));
        }
    }

    let mut pipeline_scope_ids = BTreeSet::new();
    let mut pipeline_scope_by_route_target = BTreeMap::new();
    for pipeline_scope in epd_pipeline_scopes {
        let invalid_scope =
            || SnapshotError::InvalidEpdPipelineScope(pipeline_scope.pipeline_scope_id.clone());
        if pipeline_scope.pipeline_scope_id.is_empty()
            || pipeline_scope.encoder_route_target_ids.is_empty()
            || pipeline_scope.prefill_route_target_ids.is_empty()
            || pipeline_scope.decode_route_target_ids.is_empty()
            || !pipeline_scope_ids.insert(pipeline_scope.pipeline_scope_id.clone())
        {
            return Err(invalid_scope());
        }
        let Some(reference) = epd_components_by_id.get(&pipeline_scope.encoder_route_target_ids[0])
        else {
            return Err(invalid_scope());
        };
        for (role, route_target_ids) in [
            (
                ModelServerRole::Encoder,
                pipeline_scope.encoder_route_target_ids.as_slice(),
            ),
            (
                ModelServerRole::Prefill,
                pipeline_scope.prefill_route_target_ids.as_slice(),
            ),
            (
                ModelServerRole::Decode,
                pipeline_scope.decode_route_target_ids.as_slice(),
            ),
        ] {
            for route_target_id in route_target_ids {
                let Some(component) = epd_components_by_id.get(route_target_id) else {
                    return Err(invalid_scope());
                };
                if pipeline_scope_by_route_target
                    .insert(
                        route_target_id.clone(),
                        pipeline_scope.pipeline_scope_id.clone(),
                    )
                    .is_some()
                    || component.role != role
                    || component.service_uid != reference.service_uid
                    || component.model != reference.model
                {
                    return Err(invalid_scope());
                }
            }
        }
        if aggregate_models.contains(&reference.model) || pd_models.contains(&reference.model) {
            return Err(invalid_scope());
        }
    }
    if pipeline_scope_by_route_target.len() != epd_components_by_id.len() {
        return Err(SnapshotError::InvalidEpdPipelineScope(String::new()));
    }

    // Materialize only valid scopes, retaining Pool capacity ownership and complete admission sets.
    for component in epd_components {
        let target = pool_target(
            component.service_uid.clone(),
            component.pool_uid.clone(),
            component.pool_name.clone(),
        );
        let route = RouteTarget {
            route_target_id: component.route_target_id.clone(),
            admission_targets: admission_targets(admission_by_target, &target)?,
            target,
            model: component.model,
            revision: component.revision,
            capabilities: component.capabilities,
            max_input_tokens: component.max_input_tokens,
            ready: true,
            role: component.role,
            pipeline_scope_id: Some(
                pipeline_scope_by_route_target
                    .remove(&component.route_target_id)
                    .expect("validated E/P/D scope membership"),
            ),
            data_parallel_size: component.data_parallel_size,
        };
        let component = match route.role {
            ModelServerRole::Encoder => Component::Encoder {
                endpoint: component.endpoint,
            },
            ModelServerRole::Prefill => Component::Prefill {
                endpoint: component.endpoint,
                bootstrap: Some(component.prefill_bootstrap_endpoint.ok_or_else(|| {
                    SnapshotError::IncompleteEpdComponent(route.route_target_id.clone())
                })?),
                connector: KvConnector::Mooncake,
            },
            ModelServerRole::Decode => Component::Decode {
                endpoint: component.endpoint,
                connector: KvConnector::Mooncake,
            },
            ModelServerRole::Aggregate => unreachable!("aggregate E/P/D component was rejected"),
        };
        if components
            .insert(route.route_target_id.clone(), component)
            .is_some()
        {
            return Err(SnapshotError::DuplicateRouteTarget(route.route_target_id));
        }
        routes.push(route);
    }
    Ok(())
}

fn pool_target(service_uid: String, pool_uid: String, pool_name: String) -> ScalingTarget {
    ScalingTarget {
        service_uid,
        name: pool_name,
        uid: pool_uid,
        kind: ScalingTargetKind::Pool,
    }
}

/// Resolves the controller-owned complete admission set for one emitted Pool target.
fn admission_targets(
    admission_by_target: &BTreeMap<ScalingTarget, RouteTargetSet>,
    target: &ScalingTarget,
) -> Result<RouteTargetSet, SnapshotError> {
    admission_by_target
        .get(target)
        .cloned()
        .ok_or_else(|| SnapshotError::MissingAdmissionTarget(target.uid.clone()))
}
