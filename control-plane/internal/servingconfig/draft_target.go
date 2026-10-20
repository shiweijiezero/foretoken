// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects independent Draft and Target replicas into a service-local routing scope.
package servingconfig

import (
	"fmt"
	"reflect"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
)

// PoolsHaveDT identifies cohorts that use request-bound Draft/Target execution.
func PoolsHaveDT(pools []*inferencev1alpha1.ModelPool) bool {
	return slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
		return pool.Spec.Template.SpeculationRole == inferencev1alpha1.SpeculationRoleDraft || pool.Spec.Template.SpeculationRole == inferencev1alpha1.SpeculationRoleTarget
	})
}

// ProjectServiceDTComponents publishes only committed, ready roles with a shared tokenizer.
// Target owns public model identity; Draft may load different weights.
func ProjectServiceDTComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]DTComponent, error) {
	var selected []*inferencev1alpha1.ModelGroup
	var target *inferencev1alpha1.ModelGroup
	hasDraft := false
	for _, pool := range pools {
		revision := ServingRevision(service, pool)
		if revision == "" {
			continue
		}
		role := pool.Spec.Template.SpeculationRole
		if role != inferencev1alpha1.SpeculationRoleDraft && role != inferencev1alpha1.SpeculationRoleTarget {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if !RoutingGroupOwnedBy(group, pool) || group.Spec.Revision != revision || !RoutingGroupReady(group) || group.Spec.SpeculationRole != role {
				continue
			}
			selected = append(selected, group)
			if role == inferencev1alpha1.SpeculationRoleTarget {
				target = group
			} else {
				hasDraft = true
			}
		}
	}
	if target == nil || !hasDraft {
		return nil, &SplitRoutingProjectionError{Service: service.Name, Reason: "requires at least one Ready Draft and one Ready Target ModelGroup"}
	}
	reference := target.Spec.Artifacts
	components := make([]DTComponent, 0, len(selected))
	for _, group := range selected {
		artifacts := group.Spec.Artifacts
		if artifacts.Source != reference.Source || artifacts.Tokenizer != reference.Tokenizer || artifacts.TokenizerRevision != reference.TokenizerRevision || group.Spec.SpeculationRole == inferencev1alpha1.SpeculationRoleTarget && (artifacts.Model != reference.Model || artifacts.ModelRevision != reference.ModelRevision) {
			return nil, &SplitRoutingProjectionError{Service: service.Name, Reason: fmt.Sprintf("Ready DT ModelGroup %q conflicts with the service model or tokenizer identity", group.Name)}
		}
		var revision *string
		if artifacts.Source != inferencev1alpha1.ModelSourceLocal {
			revision = &artifacts.ModelRevision
		}
		var poolForGroup *inferencev1alpha1.ModelPool
		for _, pool := range pools {
			if string(pool.UID) == group.Spec.ModelPoolRef.UID {
				poolForGroup = pool
				break
			}
		}
		components = append(components, DTComponent{
			ServiceUID: string(service.UID), PoolUID: group.Spec.ModelPoolRef.UID,
			PoolName: poolForGroup.Spec.PoolName, RouteTargetID: string(group.UID),
			PipelineScopeID: "dt:" + string(service.UID), Role: group.Spec.SpeculationRole,
			Model: reference.Model, EngineModel: artifacts.Model, EngineRevision: revision,
			MaxInputTokens: copyOptionalInt32(poolForGroup.Spec.MaxInputTokens),
			DraftPools:     slices.Clone(group.Spec.DraftPools),
			Endpoint:       workload.ModelGroupEndpoint(group, group.Spec.Runtime.Port),
		})
	}
	return components, nil
}

func equalRoutingDTComponent(left, right DTComponent) bool {
	return reflect.DeepEqual(left, right)
}

// ValidateDTModelTopology prevents one public model from mixing incompatible request protocols.
func ValidateDTModelTopology(groups []Group, pd []PDComponent, epd []EPDComponent, dt []DTComponent) error {
	other := make(map[string]bool)
	for _, group := range groups {
		other[group.Model] = true
	}
	for _, component := range pd {
		other[component.Model] = true
	}
	for _, component := range epd {
		other[component.Model] = true
	}
	for _, component := range dt {
		if other[component.Model] {
			return &RoutingIdentityConflictError{Reason: fmt.Sprintf("model %q mixes DT and other serving topologies", component.Model)}
		}
	}
	return nil
}

// ServingRoutingPools pins routing roles to committed Groups while desired Pool templates roll out.
func ServingRoutingPools(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) []*inferencev1alpha1.ModelPool {
	owned := slices.Clone(pools)
	for index, pool := range owned {
		revision := ServingRevision(service, pool)
		if revision == "" {
			continue
		}
		for groupIndex := range groups {
			group := &groups[groupIndex]
			if RoutingGroupOwnedBy(group, pool) && group.Spec.Revision == revision {
				projected := pool.DeepCopy()
				projected.Spec.Template.Role = group.Spec.Role
				projected.Spec.Template.SpeculationRole = group.Spec.SpeculationRole
				owned[index] = projected
				break
			}
		}
	}
	return owned
}
