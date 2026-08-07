// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//! Snapshot projection into routing and component inventory.

use foretoken_kv_indexer::{KvEventSourceConfig, KvRouteBinding, KvRuntimeConfig};
use foretoken_llm_facade::HttpFacade;
use foretoken_model_protocol::RuntimeEcTransferMetadata;
use foretoken_router::{BackendId, BackendRole, BackendRoute};
use std::collections::{BTreeMap, BTreeSet};
use std::sync::Arc;

use crate::registry::{Component, RouteTable, RuntimeExpectation};
use crate::snapshot::{ServingSnapshot, SnapshotError};

pub(crate) fn kv_runtime_config(
    snapshot: &ServingSnapshot,
) -> Result<KvRuntimeConfig, SnapshotError> {
    let mut sources = BTreeMap::new();
    let mut bindings = BTreeMap::new();
    for group in &snapshot.groups {
        sources.insert(
            group.backend_id.clone(),
            KvEventSourceConfig {
                backend_id: group.backend_id.clone(),
                endpoint: group.endpoint.clone(),
                scope_id: group.kv_scope_id.clone(),
            },
        );
        bindings.insert(
            group.backend_id.clone(),
            KvRouteBinding {
                source_backend_id: group.backend_id.clone(),
            },
        );
    }
    for component in &snapshot.pd_components {
        if component.role != BackendRole::Prefill {
            continue;
        }
        bindings.insert(
            component.backend_id.clone(),
            KvRouteBinding {
                source_backend_id: component.backend_id.clone(),
            },
        );
        sources.insert(
            component.backend_id.clone(),
            KvEventSourceConfig {
                backend_id: component.backend_id.clone(),
                endpoint: component.endpoint.clone(),
                scope_id: component.kv_scope_id.clone(),
            },
        );
    }
    Ok(KvRuntimeConfig {
        event_sources: sources.into_values().collect(),
        route_bindings: bindings,
    })
}
pub(crate) fn build(
    snapshot: ServingSnapshot,
) -> Result<(RouteTable, BTreeMap<BackendId, Component>), SnapshotError> {
    if snapshot.version == 0 {
        return Err(SnapshotError::InvalidVersion);
    }
    snapshot.model_identities()?;
    let mut routes = Vec::new();
    let mut components = BTreeMap::new();
    let mut aggregate_models = BTreeSet::new();
    let mut pd_models = BTreeSet::new();
    for group in snapshot.groups {
        if group.backend_id.as_str().is_empty() || group.endpoint.is_empty() {
            return Err(SnapshotError::IncompleteGroup(group.backend_id));
        }
        let facade = Arc::new(HttpFacade::new(group.endpoint.clone()).map_err(|error| {
            SnapshotError::InvalidEndpoint {
                endpoint: group.endpoint.clone(),
                message: error.to_string(),
            }
        })?);
        if components
            .insert(
                group.backend_id.clone(),
                Component::Aggregate {
                    endpoint: group.endpoint,
                    facade,
                    expected: RuntimeExpectation {
                        model: group.model.clone(),
                        revision: group.revision.clone(),
                        ec_transfer: None,
                    },
                },
            )
            .is_some()
        {
            return Err(SnapshotError::DuplicateBackend(group.backend_id));
        }
        aggregate_models.insert(group.model.clone());
        routes.push(BackendRoute {
            backend_id: group.backend_id,
            model: group.model,
            revision: group.revision,
            capabilities: group.capabilities,
            max_input_tokens: group.max_input_tokens,
            ready: true,
            role: BackendRole::Aggregate,
            domain_id: None,
        });
    }
    let mut domain_members = BTreeMap::<String, (BTreeSet<BackendId>, BTreeSet<BackendId>)>::new();
    for domain in &snapshot.pd_domains {
        domain_members.insert(
            domain.domain_id.clone(),
            (
                domain.prefill_backend_ids.iter().cloned().collect(),
                domain.decode_backend_ids.iter().cloned().collect(),
            ),
        );
    }
    for component in snapshot.pd_components {
        if component.backend_id.as_str().is_empty()
            || component.domain_id.is_empty()
            || component.endpoint.is_empty()
            || component.profile_name.is_empty()
            || component.profile_revision.is_empty()
        {
            return Err(SnapshotError::IncompletePdComponent(component.backend_id));
        }
        if component.connector != "MooncakeConnector" || component.protocol != "rdma" {
            return Err(SnapshotError::UnsupportedPdTransport(component.backend_id));
        }
        if component.role == BackendRole::Aggregate || aggregate_models.contains(&component.model) {
            return Err(SnapshotError::MixedModelRoles(component.model));
        }
        pd_models.insert(component.model.clone());
        if component.role == BackendRole::Prefill && component.prefill_bootstrap_endpoint.is_none()
        {
            return Err(SnapshotError::IncompletePdComponent(component.backend_id));
        }
        let member = domain_members
            .get(&component.domain_id)
            .ok_or_else(|| SnapshotError::InvalidPdDomain(component.domain_id.clone()))?;
        if !(if component.role == BackendRole::Prefill {
            member.0.contains(&component.backend_id)
        } else {
            member.1.contains(&component.backend_id)
        }) {
            return Err(SnapshotError::InvalidPdDomain(component.domain_id));
        }
        let expected = RuntimeExpectation {
            model: component.model.clone(),
            revision: component.revision.clone(),
            ec_transfer: None,
        };
        let route = BackendRoute {
            backend_id: component.backend_id.clone(),
            model: component.model,
            revision: component.revision,
            capabilities: component.capabilities,
            max_input_tokens: component.max_input_tokens,
            ready: true,
            role: component.role,
            domain_id: Some(component.domain_id),
        };
        let component = match route.role {
            BackendRole::Prefill => Component::Prefill {
                endpoint: component.endpoint,
                bootstrap: component
                    .prefill_bootstrap_endpoint
                    .expect("validated prefill bootstrap endpoint"),
                expected,
            },
            BackendRole::Decode => Component::Decode {
                endpoint: component.endpoint,
                expected,
            },
            BackendRole::Aggregate | BackendRole::Encoder => {
                return Err(SnapshotError::InvalidPdDomain(
                    route.domain_id.clone().unwrap_or_default(),
                ));
            }
        };
        if components
            .insert(route.backend_id.clone(), component)
            .is_some()
        {
            return Err(SnapshotError::DuplicateBackend(route.backend_id));
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
            || domain.encoder_backend_id.as_str().is_empty()
            || domain.prefill_backend_id.as_str().is_empty()
            || domain.decode_backend_id.as_str().is_empty()
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
        if component.backend_id.as_str().is_empty()
            || component.domain_id.is_empty()
            || component.endpoint.is_empty()
        {
            return Err(SnapshotError::IncompleteEpdComponent(
                component.backend_id.clone(),
            ));
        }
        let Some(domain) = epd_domains.get(&component.domain_id) else {
            return Err(SnapshotError::InvalidEpdDomain(component.domain_id.clone()));
        };
        let (index, expected_id) = match component.role {
            BackendRole::Encoder => (0, &domain.encoder_backend_id),
            BackendRole::Prefill => (1, &domain.prefill_backend_id),
            BackendRole::Decode => (2, &domain.decode_backend_id),
            BackendRole::Aggregate => {
                return Err(SnapshotError::InvalidEpdDomain(component.domain_id.clone()));
            }
        };
        if &component.backend_id != expected_id
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
            || encoder.ec_runtime_fingerprint.is_empty()
            || prefill.ec_runtime_fingerprint.is_empty()
            || encoder.ec_runtime_fingerprint != prefill.ec_runtime_fingerprint
            || !decode.ec_profile_name.is_empty()
            || !decode.ec_profile_revision.is_empty()
            || !decode.ec_connector.is_empty()
            || !decode.ec_runtime_fingerprint.is_empty()
        {
            return Err(SnapshotError::InvalidEpdDomain(domain.domain_id.clone()));
        }
    }
    for component in snapshot.epd_components {
        let expected = RuntimeExpectation {
            model: component.model.clone(),
            revision: component.revision.clone(),
            ec_transfer: match component.role {
                BackendRole::Encoder => Some(RuntimeEcTransferMetadata {
                    role: "ec_producer".into(),
                    profile: component.ec_profile_name.clone(),
                    connector: component.ec_connector.clone(),
                    fingerprint: component.ec_runtime_fingerprint.clone(),
                }),
                BackendRole::Prefill => Some(RuntimeEcTransferMetadata {
                    role: "ec_consumer".into(),
                    profile: component.ec_profile_name.clone(),
                    connector: component.ec_connector.clone(),
                    fingerprint: component.ec_runtime_fingerprint.clone(),
                }),
                BackendRole::Decode => None,
                BackendRole::Aggregate => unreachable!("aggregate E/P/D component was rejected"),
            },
        };
        let route = BackendRoute {
            backend_id: component.backend_id.clone(),
            model: component.model,
            revision: component.revision,
            capabilities: component.capabilities,
            max_input_tokens: component.max_input_tokens,
            ready: true,
            role: component.role,
            domain_id: Some(component.domain_id),
        };
        let component = match route.role {
            BackendRole::Encoder => Component::Encoder {
                endpoint: component.endpoint,
                expected,
            },
            BackendRole::Prefill => Component::Prefill {
                endpoint: component.endpoint,
                bootstrap: component.prefill_bootstrap_endpoint.ok_or_else(|| {
                    SnapshotError::IncompleteEpdComponent(route.backend_id.clone())
                })?,
                expected,
            },
            BackendRole::Decode => Component::Decode {
                endpoint: component.endpoint,
                expected,
            },
            BackendRole::Aggregate => unreachable!("aggregate E/P/D component was rejected"),
        };
        if components
            .insert(route.backend_id.clone(), component)
            .is_some()
        {
            return Err(SnapshotError::DuplicateBackend(route.backend_id));
        }
        routes.push(route);
    }
    Ok((RouteTable::new(snapshot.version, routes), components))
}
