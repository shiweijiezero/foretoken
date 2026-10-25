// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Keeps private Draft dependencies out of public routing while preserving cohort ownership.
package servingconfig

import (
	"slices"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// DraftDependenciesReady checks that a Target's selected Draft remains in the serving cohort.
// Frontend and service publication share this check; other candidates do not affect readiness.
func DraftDependenciesReady(target *api.ModelGroup, service *api.ModelService, pools []*api.ModelPool, groups []api.ModelGroup) bool {
	if target.Spec.SpeculationRole != api.SpeculationRoleTarget {
		return true
	}
	binding := target.Spec.SelectedDraftGroup()
	if binding == nil {
		return false
	}
	var pool *api.ModelPool
	for _, candidate := range pools {
		if candidate.Name == binding.PoolRef.Name && string(candidate.UID) == binding.PoolRef.UID && ModelPoolOwnedBy(candidate, service) {
			pool = candidate
			break
		}
	}
	if pool == nil || !pool.DeletionTimestamp.IsZero() || ServingRevision(service, pool) != binding.Revision {
		return false
	}
	return slices.ContainsFunc(groups, func(group api.ModelGroup) bool {
		return group.Name == binding.GroupRef.Name && string(group.UID) == binding.GroupRef.UID && ModelGroupOwnedBy(&group, pool) && group.Spec.Revision == binding.Revision && group.Spec.SpeculationRole == api.SpeculationRoleDraft && RoutingGroupReady(&group)
	})
}

// ServingSpeculationRole reads committed execution rather than a Pool template being replaced.
// Cold Pools use their compiled responsibility until a serving cohort exists.
func ServingSpeculationRole(service *api.ModelService, pool *api.ModelPool, groups []api.ModelGroup) api.SpeculationRole {
	revision := ServingRevision(service, pool)
	for index := range groups {
		group := &groups[index]
		if ModelGroupOwnedBy(group, pool) && group.Spec.Revision == revision {
			return group.Spec.SpeculationRole
		}
	}
	return pool.Spec.Template.SpeculationRole
}
