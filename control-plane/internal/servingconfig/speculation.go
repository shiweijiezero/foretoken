// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Keeps private Draft dependencies out of public routing while preserving cohort ownership.
package servingconfig

import (
	"slices"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// DraftDependenciesReady checks that every Draft in a Target's frozen shared pool remains serving.
// Frontend and service publication share this check so a Target never advertises a partial pool.
func DraftDependenciesReady(target *api.ModelGroup, service *api.ModelService, pools []*api.ModelPool, groups []api.ModelGroup) bool {
	if target.Spec.SpeculationRole != api.SpeculationRoleTarget {
		return true
	}
	if len(target.Spec.DraftGroups) == 0 {
		return false
	}
	for _, binding := range target.Spec.DraftGroups {
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
		if !slices.ContainsFunc(groups, func(group api.ModelGroup) bool {
			return group.Name == binding.GroupRef.Name && string(group.UID) == binding.GroupRef.UID && ModelGroupOwnedBy(&group, pool) && group.Spec.Revision == binding.Revision && group.Spec.SpeculationRole == api.SpeculationRoleDraft && RoutingGroupReady(&group)
		}) {
			return false
		}
	}
	return true
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
