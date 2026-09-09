// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Snapshot projection into routing and component inventory.

use foretoken_kv_indexer::{KvEventSourceConfig, KvRouteBinding, KvRuntimeConfig};
use foretoken_llm_facade::HttpFacade;
use foretoken_model_protocol::ModelServerRole;
use foretoken_model_protocol::{KvCacheLocality, KvPlacement, KvStorageTier};
use foretoken_router::{
    ModelRouteTable, RouteTarget, RouteTargetId, RouteTargetSet, ScalingTarget, ScalingTargetKind,
};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use crate::registry::Component;
use crate::snapshot::{ServingSnapshot, SnapshotError};

/// Projects prompt-side KV event sources and route bindings from a serving snapshot.
///
/// `BackendRegistryBuild` consumes this derived runtime configuration while the snapshot remains controller-owned.
pub(crate) fn project_kv_runtime(
    snapshot: &ServingSnapshot,
) -> Result<KvRuntimeConfig, SnapshotError> {
    let mut sources = BTreeMap::new();
    let mut bindings = BTreeMap::new();

    // Aggregate and prefill runtimes are the only components that publish prompt-side
    // KV events. Each data-parallel rank gets an independent source and route binding.
    let mut add_route = |route_target_id: &RouteTargetId,
                         endpoint: &str,
                         model_revision: &str,
                         scope_id: &str,
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
                    spec_kind: "full_attention".into(),
                    sliding_window: None,
                    group_idx: None,
                },
            );
            rank_sources.insert(dp_rank, event_source_id);
        }
        bindings.insert(
            route_target_id.as_str().to_owned(),
            KvRouteBinding {
                data_parallel_rank_event_source_ids: rank_sources,
                readable_placements: [KvPlacement {
                    tier: KvStorageTier::Device,
                    locality: KvCacheLocality::Local,
                }]
                .into_iter()
                .collect(),
                can_restore_or_transfer: false,
            },
        );
    };
    for group in &snapshot.groups {
        add_route(
            &group.route_target_id,
            &group.endpoint,
            &group.revision,
            &group.kv_scope_id,
            group.data_parallel_size,
        );
    }
    for component in &snapshot.pd_components {
        if component.role == ModelServerRole::Prefill {
            add_route(
                &component.route_target_id,
                &component.endpoint,
                &component.revision,
                &component.kv_scope_id,
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

fn pool_target(service_uid: String, pool_uid: String, pool_name: String) -> ScalingTarget {
    ScalingTarget {
        service_uid,
        name: pool_name,
        uid: pool_uid,
        kind: ScalingTargetKind::Pool,
    }
}

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
    let admission_targets = |target: &ScalingTarget| {
        admission_by_target
            .get(target)
            .cloned()
            .ok_or_else(|| SnapshotError::MissingAdmissionTarget(target.uid.clone()))
    };
    let mut routes = Vec::new();
    let mut components = BTreeMap::new();
    let mut aggregate_models = BTreeSet::new();
    let mut pd_models = BTreeSet::new();

    // Aggregate groups map one physical model-server endpoint directly to one routing
    // target and one reusable HTTP facade.
    for group in snapshot.groups {
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
            admission_targets: admission_targets(&target)?,
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
    // P/D components are admitted independently by Pool, but routing is valid only when
    // every component belongs to the explicit prefill/decode membership of its pipeline.
    let mut pipeline_scope_members =
        BTreeMap::<String, (BTreeSet<RouteTargetId>, BTreeSet<RouteTargetId>)>::new();
    for pipeline_scope in &snapshot.pd_pipeline_scopes {
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
    for component in snapshot.pd_components {
        if component.service_uid.is_empty()
            || component.pool_uid.is_empty()
            || component.pool_name.is_empty()
            || component.route_target_id.as_str().is_empty()
            || component.pipeline_scope_id.is_empty()
            || component.endpoint.is_empty()
            || component.profile_name.is_empty()
            || component.profile_revision.is_empty()
            || component.kv_scope_id.is_empty()
        {
            return Err(SnapshotError::IncompletePdComponent(
                component.route_target_id,
            ));
        }
        if component.connector != "MooncakeConnector" || component.protocol != "rdma" {
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
            admission_targets: admission_targets(&target)?,
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
                bootstrap: component
                    .prefill_bootstrap_endpoint
                    .expect("validated prefill bootstrap endpoint"),
            },
            ModelServerRole::Decode => Component::Decode {
                endpoint: component.endpoint,
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

    // E/P/D scopes list every route that can participate in the same connector-compatible
    // service pipeline. Algorithms may choose any E, P, and D combination within that boundary.
    let mut epd_pipeline_scopes = BTreeMap::new();
    for pipeline_scope in &snapshot.epd_pipeline_scopes {
        let members = (
            pipeline_scope
                .encoder_route_target_ids
                .iter()
                .cloned()
                .collect::<BTreeSet<_>>(),
            pipeline_scope
                .prefill_route_target_ids
                .iter()
                .cloned()
                .collect::<BTreeSet<_>>(),
            pipeline_scope
                .decode_route_target_ids
                .iter()
                .cloned()
                .collect::<BTreeSet<_>>(),
        );
        if pipeline_scope.pipeline_scope_id.is_empty()
            || members.0.is_empty()
            || members.1.is_empty()
            || members.2.is_empty()
            || members.0.len() != pipeline_scope.encoder_route_target_ids.len()
            || members.1.len() != pipeline_scope.prefill_route_target_ids.len()
            || members.2.len() != pipeline_scope.decode_route_target_ids.len()
            || epd_pipeline_scopes
                .insert(pipeline_scope.pipeline_scope_id.clone(), members)
                .is_some()
        {
            return Err(SnapshotError::InvalidEpdPipelineScope(
                pipeline_scope.pipeline_scope_id.clone(),
            ));
        }
    }
    let mut epd_pipeline_scope_members = BTreeMap::<
        String,
        (
            Vec<&crate::snapshot::SnapshotEpdComponent>,
            Vec<&crate::snapshot::SnapshotEpdComponent>,
            Vec<&crate::snapshot::SnapshotEpdComponent>,
        ),
    >::new();
    let mut epd_route_target_ids = BTreeSet::new();
    for component in &snapshot.epd_components {
        if component.service_uid.is_empty()
            || component.pool_uid.is_empty()
            || component.pool_name.is_empty()
            || component.route_target_id.as_str().is_empty()
            || component.pipeline_scope_id.is_empty()
            || component.endpoint.is_empty()
            || component.kv_scope_id.is_empty()
        {
            return Err(SnapshotError::IncompleteEpdComponent(
                component.route_target_id.clone(),
            ));
        }
        if !epd_route_target_ids.insert(component.route_target_id.clone()) {
            return Err(SnapshotError::DuplicateRouteTarget(
                component.route_target_id.clone(),
            ));
        }
        let Some(declared) = epd_pipeline_scopes.get(&component.pipeline_scope_id) else {
            return Err(SnapshotError::InvalidEpdPipelineScope(
                component.pipeline_scope_id.clone(),
            ));
        };
        let members = epd_pipeline_scope_members
            .entry(component.pipeline_scope_id.clone())
            .or_default();
        match component.role {
            ModelServerRole::Encoder if declared.0.contains(&component.route_target_id) => {
                members.0.push(component)
            }
            ModelServerRole::Prefill if declared.1.contains(&component.route_target_id) => {
                members.1.push(component)
            }
            ModelServerRole::Decode if declared.2.contains(&component.route_target_id) => {
                members.2.push(component)
            }
            ModelServerRole::Aggregate
            | ModelServerRole::Encoder
            | ModelServerRole::Prefill
            | ModelServerRole::Decode => {
                return Err(SnapshotError::InvalidEpdPipelineScope(
                    component.pipeline_scope_id.clone(),
                ));
            }
        }
    }
    for (pipeline_scope_id, declared) in &epd_pipeline_scopes {
        let Some((encoders, prefills, decodes)) = epd_pipeline_scope_members.get(pipeline_scope_id)
        else {
            return Err(SnapshotError::InvalidEpdPipelineScope(
                pipeline_scope_id.clone(),
            ));
        };
        let observed = (
            encoders
                .iter()
                .map(|component| component.route_target_id.clone())
                .collect::<BTreeSet<_>>(),
            prefills
                .iter()
                .map(|component| component.route_target_id.clone())
                .collect::<BTreeSet<_>>(),
            decodes
                .iter()
                .map(|component| component.route_target_id.clone())
                .collect::<BTreeSet<_>>(),
        );
        if &observed != declared {
            return Err(SnapshotError::InvalidEpdPipelineScope(
                pipeline_scope_id.clone(),
            ));
        }
        let reference_encoder = encoders[0];
        let reference_prefill = prefills[0];
        let reference_decode = decodes[0];
        if aggregate_models.contains(&reference_encoder.model)
            || pd_models.contains(&reference_encoder.model)
            || encoders
                .iter()
                .any(|encoder| !compatible_encoder_prefill(encoder, reference_prefill))
            || prefills.iter().any(|prefill| {
                !compatible_encoder_prefill(reference_encoder, prefill)
                    || !compatible_prefill_decode(prefill, reference_decode)
            })
            || decodes
                .iter()
                .any(|decode| !compatible_prefill_decode(reference_prefill, decode))
        {
            return Err(SnapshotError::InvalidEpdPipelineScope(
                pipeline_scope_id.clone(),
            ));
        }
    }
    // Only fully validated compatibility scopes are materialized into executable components and
    // service-scoped admission targets.
    for component in snapshot.epd_components {
        let target = ScalingTarget {
            uid: component.service_uid.clone(),
            service_uid: component.service_uid.clone(),
            name: "epd".into(),
            kind: ScalingTargetKind::EPDPipelineScope,
        };
        let route = RouteTarget {
            route_target_id: component.route_target_id.clone(),
            admission_targets: admission_targets(&target)?,
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
            ModelServerRole::Encoder => Component::Encoder {
                endpoint: component.endpoint,
            },
            ModelServerRole::Prefill => Component::Prefill {
                endpoint: component.endpoint,
                bootstrap: component.prefill_bootstrap_endpoint.ok_or_else(|| {
                    SnapshotError::IncompleteEpdComponent(route.route_target_id.clone())
                })?,
            },
            ModelServerRole::Decode => Component::Decode {
                endpoint: component.endpoint,
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
    Ok((ModelRouteTable::new(routes), components))
}

fn compatible_encoder_prefill(
    encoder: &crate::snapshot::SnapshotEpdComponent,
    prefill: &crate::snapshot::SnapshotEpdComponent,
) -> bool {
    encoder.service_uid == prefill.service_uid
        && encoder.source == prefill.source
        && encoder.model == prefill.model
        && encoder.revision == prefill.revision
        && encoder.tokenizer == prefill.tokenizer
        && encoder.tokenizer_revision == prefill.tokenizer_revision
        && encoder.profile_name.is_empty()
        && encoder.profile_revision.is_empty()
        && encoder.connector.is_empty()
        && encoder.protocol.is_empty()
        && !encoder.ec_profile_name.is_empty()
        && encoder.ec_profile_name == prefill.ec_profile_name
        && !encoder.ec_profile_revision.is_empty()
        && encoder.ec_profile_revision == prefill.ec_profile_revision
        && encoder.ec_connector == "ECExampleConnector"
        && encoder.ec_connector == prefill.ec_connector
}

fn compatible_prefill_decode(
    prefill: &crate::snapshot::SnapshotEpdComponent,
    decode: &crate::snapshot::SnapshotEpdComponent,
) -> bool {
    prefill.service_uid == decode.service_uid
        && prefill.source == decode.source
        && prefill.model == decode.model
        && prefill.revision == decode.revision
        && prefill.tokenizer == decode.tokenizer
        && prefill.tokenizer_revision == decode.tokenizer_revision
        && !prefill.profile_name.is_empty()
        && prefill.profile_name == decode.profile_name
        && !prefill.profile_revision.is_empty()
        && prefill.profile_revision == decode.profile_revision
        && prefill.connector == "MooncakeConnector"
        && prefill.connector == decode.connector
        && prefill.protocol == "rdma"
        && prefill.protocol == decode.protocol
        && prefill.kv_scope_id == decode.kv_scope_id
        && prefill.prefill_bootstrap_endpoint.is_some()
        && !prefill.ec_profile_name.is_empty()
        && !prefill.ec_profile_revision.is_empty()
        && prefill.ec_connector == "ECExampleConnector"
        && decode.ec_profile_name.is_empty()
        && decode.ec_profile_revision.is_empty()
        && decode.ec_connector.is_empty()
}
