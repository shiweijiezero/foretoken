// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! Snapshot projection into routing and component inventory.

use foretoken_kv_indexer::{KvEventSourceConfig, KvRouteBinding, KvRuntimeConfig};
use foretoken_llm_facade::HttpFacade;
use foretoken_model_protocol::ModelServerRole;
use foretoken_model_protocol::{KvCacheLocality, KvPlacement, KvStorageTier};
use foretoken_router::{RouteTarget, RouteTargetId, ScalingTarget, ScalingTargetKind};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use crate::registry::{Component, RouteTable};
use crate::snapshot::{ServingSnapshot, SnapshotError};

pub(crate) fn kv_runtime_config(
    snapshot: &ServingSnapshot,
) -> Result<KvRuntimeConfig, SnapshotError> {
    let mut sources = BTreeMap::new();
    let mut bindings = BTreeMap::new();
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

fn epd_target(service_uid: String) -> ScalingTarget {
    ScalingTarget {
        uid: service_uid.clone(),
        service_uid,
        name: "epd".into(),
        kind: ScalingTargetKind::EPDDomain,
    }
}

pub(crate) fn build(
    snapshot: ServingSnapshot,
) -> Result<(RouteTable, BTreeMap<RouteTargetId, Component>), SnapshotError> {
    if snapshot.version == 0 {
        return Err(SnapshotError::InvalidVersion);
    }
    snapshot.model_identities()?;
    let mut routes = Vec::new();
    let mut components = BTreeMap::new();
    let mut aggregate_models = BTreeSet::new();
    let mut pd_models = BTreeSet::new();
    for group in snapshot.groups {
        if group.service_uid.is_empty()
            || group.pool_uid.is_empty()
            || group.pool_name.is_empty()
            || group.route_target_id.as_str().is_empty()
            || group.endpoint.is_empty()
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
        routes.push(RouteTarget {
            route_target_id: group.route_target_id,
            target: pool_target(group.service_uid, group.pool_uid, group.pool_name),
            model: group.model,
            revision: group.revision,
            capabilities: group.capabilities,
            max_input_tokens: group.max_input_tokens,
            ready: true,
            role: ModelServerRole::Aggregate,
            domain_id: None,
            data_parallel_size: group.data_parallel_size,
        });
    }
    let mut domain_members =
        BTreeMap::<String, (BTreeSet<RouteTargetId>, BTreeSet<RouteTargetId>)>::new();
    for domain in &snapshot.pd_domains {
        domain_members.insert(
            domain.domain_id.clone(),
            (
                domain.prefill_route_target_ids.iter().cloned().collect(),
                domain.decode_route_target_ids.iter().cloned().collect(),
            ),
        );
    }
    for component in snapshot.pd_components {
        if component.service_uid.is_empty()
            || component.pool_uid.is_empty()
            || component.pool_name.is_empty()
            || component.route_target_id.as_str().is_empty()
            || component.domain_id.is_empty()
            || component.endpoint.is_empty()
            || component.profile_name.is_empty()
            || component.profile_revision.is_empty()
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
        let member = domain_members
            .get(&component.domain_id)
            .ok_or_else(|| SnapshotError::InvalidPdDomain(component.domain_id.clone()))?;
        if !(if component.role == ModelServerRole::Prefill {
            member.0.contains(&component.route_target_id)
        } else {
            member.1.contains(&component.route_target_id)
        }) {
            return Err(SnapshotError::InvalidPdDomain(component.domain_id));
        }
        let route = RouteTarget {
            route_target_id: component.route_target_id.clone(),
            target: pool_target(
                component.service_uid.clone(),
                component.pool_uid.clone(),
                component.pool_name.clone(),
            ),
            model: component.model,
            revision: component.revision,
            capabilities: component.capabilities,
            max_input_tokens: component.max_input_tokens,
            ready: true,
            role: component.role,
            domain_id: Some(component.domain_id),
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
                return Err(SnapshotError::InvalidPdDomain(
                    route.domain_id.clone().unwrap_or_default(),
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
    for (domain, (p, d)) in domain_members {
        if p.is_empty() || d.is_empty() {
            return Err(SnapshotError::InvalidPdDomain(domain));
        }
    }

    let mut epd_domains = BTreeMap::new();
    for domain in &snapshot.epd_domains {
        if domain.domain_id.is_empty()
            || domain.encoder_route_target_id.as_str().is_empty()
            || domain.prefill_route_target_id.as_str().is_empty()
            || domain.decode_route_target_id.as_str().is_empty()
            || epd_domains
                .insert(domain.domain_id.clone(), domain)
                .is_some()
        {
            return Err(SnapshotError::InvalidEpdDomain(domain.domain_id.clone()));
        }
    }
    let mut epd_members =
        BTreeMap::<String, [Option<&crate::snapshot::SnapshotEpdComponent>; 3]>::new();
    for component in &snapshot.epd_components {
        if component.service_uid.is_empty()
            || component.pool_uid.is_empty()
            || component.pool_name.is_empty()
            || component.route_target_id.as_str().is_empty()
            || component.domain_id.is_empty()
            || component.endpoint.is_empty()
        {
            return Err(SnapshotError::IncompleteEpdComponent(
                component.route_target_id.clone(),
            ));
        }
        let Some(domain) = epd_domains.get(&component.domain_id) else {
            return Err(SnapshotError::InvalidEpdDomain(component.domain_id.clone()));
        };
        let (index, expected_id) = match component.role {
            ModelServerRole::Encoder => (0, &domain.encoder_route_target_id),
            ModelServerRole::Prefill => (1, &domain.prefill_route_target_id),
            ModelServerRole::Decode => (2, &domain.decode_route_target_id),
            ModelServerRole::Aggregate => {
                return Err(SnapshotError::InvalidEpdDomain(component.domain_id.clone()));
            }
        };
        if &component.route_target_id != expected_id
            || epd_members
                .entry(component.domain_id.clone())
                .or_insert([None, None, None])[index]
                .replace(component)
                .is_some()
        {
            return Err(SnapshotError::InvalidEpdDomain(component.domain_id.clone()));
        }
    }
    for (domain_id, domain) in &epd_domains {
        let Some([Some(encoder), Some(prefill), Some(decode)]) = epd_members.get(domain_id) else {
            return Err(SnapshotError::InvalidEpdDomain(domain_id.clone()));
        };
        if aggregate_models.contains(&encoder.model)
            || pd_models.contains(&encoder.model)
            || encoder.model != prefill.model
            || encoder.model != decode.model
            || encoder.revision != prefill.revision
            || encoder.revision != decode.revision
            || encoder.tokenizer != prefill.tokenizer
            || encoder.tokenizer != decode.tokenizer
            || encoder.tokenizer_revision != prefill.tokenizer_revision
            || encoder.tokenizer_revision != decode.tokenizer_revision
            || !encoder.profile_name.is_empty()
            || !encoder.profile_revision.is_empty()
            || !encoder.connector.is_empty()
            || !encoder.protocol.is_empty()
            || prefill.profile_name.is_empty()
            || decode.profile_name.is_empty()
            || prefill.profile_name != decode.profile_name
            || prefill.profile_revision.is_empty()
            || decode.profile_revision.is_empty()
            || prefill.profile_revision != decode.profile_revision
            || prefill.connector != "MooncakeConnector"
            || decode.connector != "MooncakeConnector"
            || prefill.protocol != "rdma"
            || decode.protocol != "rdma"
            || prefill.kv_scope_id.is_empty()
            || decode.kv_scope_id.is_empty()
            || prefill.kv_scope_id != decode.kv_scope_id
            || encoder.ec_profile_name.is_empty()
            || prefill.ec_profile_name.is_empty()
            || encoder.ec_profile_name != prefill.ec_profile_name
            || encoder.ec_profile_revision.is_empty()
            || prefill.ec_profile_revision.is_empty()
            || encoder.ec_profile_revision != prefill.ec_profile_revision
            || encoder.ec_connector != "ECExampleConnector"
            || prefill.ec_connector != "ECExampleConnector"
            || encoder.ec_connector != prefill.ec_connector
            || !decode.ec_profile_name.is_empty()
            || !decode.ec_profile_revision.is_empty()
            || !decode.ec_connector.is_empty()
        {
            return Err(SnapshotError::InvalidEpdDomain(domain.domain_id.clone()));
        }
    }
    for component in snapshot.epd_components {
        let route = RouteTarget {
            route_target_id: component.route_target_id.clone(),
            target: epd_target(component.service_uid.clone()),
            model: component.model,
            revision: component.revision,
            capabilities: component.capabilities,
            max_input_tokens: component.max_input_tokens,
            ready: true,
            role: component.role,
            domain_id: Some(component.domain_id),
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
    Ok((RouteTable::new(snapshot.version, routes), components))
}
