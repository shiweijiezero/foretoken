// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Selects currently routable execution groups for frontend publication.

package frontend

import (
	"cmp"
	"errors"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
)

// projectableRouting publishes service-selected cohorts whose Groups satisfy ownership,
// readiness, and aggregate or split-topology compatibility.
func projectableRouting(services []inferencev1alpha1.ModelService, pools []inferencev1alpha1.ModelPool, modelGroups []inferencev1alpha1.ModelGroup) ([]servingconfig.Group, []servingconfig.PDComponent, []servingconfig.PDPipelineScope, []servingconfig.EPDComponent, []servingconfig.EPDPipelineScope, error) {

	groups := make([]servingconfig.Group, 0, len(modelGroups))
	pdComponents := make([]servingconfig.PDComponent, 0)
	pdPipelineScopes := make([]servingconfig.PDPipelineScope, 0)
	epdComponents := make([]servingconfig.EPDComponent, 0)
	epdPipelineScopes := make([]servingconfig.EPDPipelineScope, 0)
	var projectionErr error
	for serviceIndex := range services {
		service := &services[serviceIndex]
		if (service.Spec.Backend != "vllm" && service.Spec.Backend != "vllm-omni") || !servingconfig.HasServingCohort(service) {
			continue
		}
		servicePools := servingconfig.OwnedRoutingPools(service, pools)
		servicePools = slices.DeleteFunc(servicePools, func(pool *inferencev1alpha1.ModelPool) bool {
			return servingconfig.ServingRevision(service, pool) == ""
		})
		if servingconfig.PoolsHaveEPD(servicePools) {
			components, pipelineScopes, err := servingconfig.ProjectServiceEPDComponents(service, servicePools, modelGroups)
			if err != nil {
				// An incomplete E/P/D Service must not withdraw other Services' routes.
				projectionErr = errors.Join(projectionErr, err)
				continue
			}
			epdComponents = append(epdComponents, components...)
			epdPipelineScopes = append(epdPipelineScopes, pipelineScopes...)
			continue
		}
		if servingconfig.PoolsHavePD(servicePools) {
			components, pipelineScope, err := servingconfig.ProjectServicePDComponents(service, servicePools, modelGroups)
			if err != nil {
				// A transiently incomplete P/D Service must not withdraw other Services' routes.
				projectionErr = errors.Join(projectionErr, err)
				continue
			}
			pdComponents = append(pdComponents, components...)
			pdPipelineScopes = append(pdPipelineScopes, pipelineScope)
			continue
		}
		for poolIndex := range servicePools {
			pool := servicePools[poolIndex]
			if servingconfig.ServingRevision(service, pool) == "" {
				continue
			}
			for groupIndex := range modelGroups {
				group := &modelGroups[groupIndex]
				if !servingconfig.RoutingGroupOwnedBy(group, pool) || group.Spec.Revision != servingconfig.ServingRevision(service, pool) || !servingconfig.RoutingGroupReady(group) || group.Spec.Role != inferencev1alpha1.ModelRoleAggregate {
					continue
				}
				groups = append(groups, servingconfig.RoutingGroupForService(service, pool, group))
			}
		}
	}
	slices.SortFunc(groups, func(left, right servingconfig.Group) int {
		return cmp.Compare(left.RouteTargetID, right.RouteTargetID)
	})
	slices.SortFunc(pdComponents, func(left, right servingconfig.PDComponent) int {
		return cmp.Compare(left.RouteTargetID, right.RouteTargetID)
	})
	slices.SortFunc(pdPipelineScopes, func(left, right servingconfig.PDPipelineScope) int {
		return cmp.Compare(left.PipelineScopeID, right.PipelineScopeID)
	})
	slices.SortFunc(epdComponents, func(left, right servingconfig.EPDComponent) int {
		return cmp.Compare(left.RouteTargetID, right.RouteTargetID)
	})
	slices.SortFunc(epdPipelineScopes, func(left, right servingconfig.EPDPipelineScope) int {
		return cmp.Compare(left.PipelineScopeID, right.PipelineScopeID)
	})
	if err := servingconfig.ValidateRoutingIdentities(nil, groups, pdComponents, epdComponents); err != nil {
		return nil, nil, nil, nil, nil, err
	}
	return groups, pdComponents, pdPipelineScopes, epdComponents, epdPipelineScopes, projectionErr
}
