// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

//! DT discovery validates service identity separately from each role's loaded weights.

use std::collections::{BTreeMap, BTreeSet};

use foretoken_llm_facade::draft_target::RoleClient;
use foretoken_model_protocol::ModelServerRole;
use foretoken_router::{RouteTarget, RouteTargetId, RouteTargetSet, ScalingTarget};

use crate::registry::Component;
use crate::snapshot::{ServingSnapshot, SnapshotError};
use crate::snapshot_projection::pool_target;

/// Materializes independent DT routes with one admission set and tokenizer per scope.
/// Registry construction owns the resulting clients; no Draft endpoint is embedded in Target.
pub(crate) fn project(
    snapshot: &ServingSnapshot,
    admission: &BTreeMap<ScalingTarget, RouteTargetSet>,
    routes: &mut Vec<RouteTarget>,
    components: &mut BTreeMap<RouteTargetId, Component>,
) -> Result<(), SnapshotError> {
    let mut scopes = BTreeMap::<&str, (&str, &str, RouteTargetSet, bool, bool)>::new();
    for role in &snapshot.dt_components {
        let invalid = || SnapshotError::InvalidDtComponent(role.route_target_id.as_str().into());
        let model = snapshot
            .models
            .iter()
            .find(|model| model.service_uid == role.service_uid && model.model == role.model)
            .ok_or_else(invalid)?;
        if role.service_uid.is_empty()
            || role.pool_uid.is_empty()
            || role.pool_name.is_empty()
            || role.route_target_id.as_str().is_empty()
            || role.pipeline_scope_id.is_empty()
            || role.engine_model.is_empty()
            || (role.role == ModelServerRole::Target
                && (role.engine_model != model.model
                    || role.engine_revision
                        != (model.source != crate::ModelSource::Local)
                            .then(|| model.revision.clone())))
            || !matches!(role.role, ModelServerRole::Draft | ModelServerRole::Target)
            || snapshot
                .groups
                .iter()
                .any(|group| group.model == role.model)
            || snapshot.pd_components.iter().any(|component| {
                component.model == role.model
                    || component.pipeline_scope_id == role.pipeline_scope_id
            })
            || snapshot
                .epd_components
                .iter()
                .any(|component| component.model == role.model)
            || snapshot
                .epd_pipeline_scopes
                .iter()
                .any(|scope| scope.pipeline_scope_id == role.pipeline_scope_id)
        {
            return Err(invalid());
        }
        let target = pool_target(
            role.service_uid.clone(),
            role.pool_uid.clone(),
            role.pool_name.clone(),
        );
        let targets = admission.get(&target).ok_or_else(invalid)?;
        let scope = scopes.entry(&role.pipeline_scope_id).or_insert_with(|| {
            (
                &role.service_uid,
                &role.model,
                targets.clone(),
                false,
                false,
            )
        });
        if scope.0 != role.service_uid || scope.1 != role.model || &scope.2 != targets {
            return Err(invalid());
        }
        match role.role {
            ModelServerRole::Draft => scope.3 = true,
            ModelServerRole::Target => scope.4 = true,
            _ => unreachable!("validated DT role"),
        }
        // Local snapshots have no repository revision. Remote tokenizer identity is pinned by
        // the service model, and both role health reports must match it before becoming routable.
        let tokenizer_revision =
            (model.source != crate::ModelSource::Local).then(|| model.tokenizer_revision.clone());
        let client = RoleClient::new(role.endpoint.clone()).map_err(|error| {
            SnapshotError::InvalidEndpoint {
                endpoint: role.endpoint.clone(),
                message: error.to_string(),
            }
        })?;
        if components
            .insert(
                role.route_target_id.clone(),
                Component::DraftTarget {
                    endpoint: role.endpoint.clone(),
                    client,
                    role: role.role,
                    model: role.engine_model.clone(),
                    revision: role.engine_revision.clone(),
                    tokenizer: model.tokenizer.clone(),
                    tokenizer_revision,
                },
            )
            .is_some()
        {
            return Err(SnapshotError::DuplicateRouteTarget(
                role.route_target_id.clone(),
            ));
        }
        routes.push(RouteTarget {
            route_target_id: role.route_target_id.clone(),
            target,
            admission_targets: targets.clone(),
            model: model.model.clone(),
            revision: model.revision.clone(),
            capabilities: BTreeSet::new(),
            max_input_tokens: role.max_input_tokens,
            ready: true,
            role: role.role,
            pipeline_scope_id: Some(role.pipeline_scope_id.clone()),
            data_parallel_size: 1,
        });
    }
    for (scope, (_, _, _, draft, target)) in scopes {
        if !draft || !target {
            return Err(SnapshotError::InvalidDtComponent(scope.into()));
        }
    }
    Ok(())
}
