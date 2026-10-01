// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects independent Draft and Target replicas into a service-local routing scope.
package controllers

import (
	"fmt"
	"reflect"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func poolsHaveDT(pools []*inferencev1alpha1.ModelPool) bool {
	return slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
		return pool.Spec.Template.SpeculationRole == inferencev1alpha1.SpeculationRoleDraft || pool.Spec.Template.SpeculationRole == inferencev1alpha1.SpeculationRoleTarget
	})
}

// projectServiceDTComponents publishes only committed, ready roles with a shared tokenizer.
// Target owns public model identity; Draft may load different weights.
func projectServiceDTComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]servingSnapshotDTComponent, error) {
	var selected []*inferencev1alpha1.ModelGroup
	var target *inferencev1alpha1.ModelGroup
	hasDraft := false
	for _, pool := range pools {
		revision := serviceServingRevision(service, pool)
		if revision == "" {
			continue
		}
		role := pool.Spec.Template.SpeculationRole
		if role != inferencev1alpha1.SpeculationRoleDraft && role != inferencev1alpha1.SpeculationRoleTarget {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if !routingGroupOwnedBy(group, pool) || group.Spec.Revision != revision || !routingGroupReady(group) || group.Spec.SpeculationRole != role {
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
		return nil, &splitRoutingProjectionError{service: service.Name, reason: "requires at least one Ready Draft and one Ready Target ModelGroup"}
	}
	reference := target.Spec.Artifacts
	components := make([]servingSnapshotDTComponent, 0, len(selected))
	for _, group := range selected {
		artifacts := group.Spec.Artifacts
		if artifacts.Source != reference.Source || artifacts.Tokenizer != reference.Tokenizer || artifacts.TokenizerRevision != reference.TokenizerRevision || group.Spec.SpeculationRole == inferencev1alpha1.SpeculationRoleTarget && (artifacts.Model != reference.Model || artifacts.ModelRevision != reference.ModelRevision) {
			return nil, &splitRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("Ready DT ModelGroup %q conflicts with the service model or tokenizer identity", group.Name)}
		}
		var revision *string
		if artifacts.Source != inferencev1alpha1.ModelSourceLocal {
			revision = &artifacts.ModelRevision
		}
		components = append(components, servingSnapshotDTComponent{
			ServiceUID: string(service.UID), PoolUID: group.Spec.ModelPoolRef.UID,
			PoolName: routingPoolName(pools, group), RouteTargetID: string(group.UID),
			PipelineScopeID: "dt:" + string(service.UID), Role: group.Spec.SpeculationRole,
			Model: reference.Model, EngineModel: artifacts.Model, EngineRevision: revision,
			MaxInputTokens: copyOptionalInt32(group.Spec.MaxInputTokens),
			Endpoint:       modelGroupEndpoint(group, group.Spec.Runtime.Port),
		})
	}
	return components, nil
}

func equalRoutingDTComponent(left, right servingSnapshotDTComponent) bool {
	return reflect.DeepEqual(left, right)
}

// validateDTModelTopology prevents one public model from mixing incompatible request protocols.
func validateDTModelTopology(groups []servingSnapshotGroup, pd []servingSnapshotPDComponent, epd []servingSnapshotEPDComponent, dt []servingSnapshotDTComponent) error {
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
			return &routingIdentityConflictError{reason: fmt.Sprintf("model %q mixes DT and other serving topologies", component.Model)}
		}
	}
	return nil
}
