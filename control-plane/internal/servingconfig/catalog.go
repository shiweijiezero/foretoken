// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves committed public model identity for catalog and profiling consumers.
package servingconfig

import (
	"fmt"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// SelectedCatalogIdentity reads immutable selected artifacts, including draining Groups.
// Only the same published cohort may supply identity after its artifacts disappear.
func SelectedCatalogIdentity(service *inferencev1alpha1.ModelService, pools []inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, previousModels []Model) (Group, string, error) {
	selectedRevisions := SortedServingPoolRevisions(service.Status.ServingPoolRevisions)
	var retained *Model
	for index := range previousModels {
		model := &previousModels[index]
		if model.ServiceUID == string(service.UID) && model.Topology != "" && slices.Equal(model.SelectedPoolRevisions, selectedRevisions) {
			retained = model
			break
		}
	}
	var identities []Group
	var roles []inferencev1alpha1.ModelRole
	var missing []string
	isDT := false
	for _, selected := range selectedRevisions {
		found := false
		for poolIndex := range pools {
			pool := &pools[poolIndex]
			if !ModelPoolOwnedBy(pool, service) || string(pool.UID) != selected.PoolUID || pool.Spec.PoolName != selected.PoolName {
				continue
			}
			for groupIndex := range groups {
				group := &groups[groupIndex]
				if !ModelGroupOwnedBy(group, pool) || group.Spec.Revision != selected.Revision {
					continue
				}
				found = true
				if group.Spec.SpeculationRole != "" {
					isDT = true
				}
				if group.Spec.SpeculationRole == inferencev1alpha1.SpeculationRoleDraft {
					continue
				}
				identity := RoutingGroupForService(service, pool, group)
				if group.Spec.Role == inferencev1alpha1.ModelRolePrefill || group.Spec.Role == inferencev1alpha1.ModelRoleDecode {
					features := group.Spec.Features
					features.Multimodal = nil
					identity.Capabilities = RoutingCapabilities(features)
				}
				identities = append(identities, identity)
				roles = append(roles, group.Spec.Role)
			}
		}
		if !found {
			missing = append(missing, fmt.Sprintf("Pool %q revision %q", selected.PoolName, selected.Revision))
		}
	}
	topology := CatalogTopology(roles)
	if isDT {
		topology = "D/T"
	}
	if retained != nil {
		// Missing stages keep their published topology; surviving stages must fit it.
		if len(roles) > 0 && topology != retained.Topology && !(len(missing) > 0 && topology == "P/D" && retained.Topology == "E/P/D") {
			return Group{}, "", &RoutingIdentityConflictError{Reason: fmt.Sprintf("ModelService %q selected artifacts conflict with its published topology", service.Name)}
		}
		topology = retained.Topology
		published := Group{
			Model: retained.Model, Source: retained.Source, Revision: retained.Revision,
			Tokenizer: retained.Tokenizer, TokenizerRevision: retained.TokenizerRevision,
			PrefixCacheIsolation: retained.PrefixCacheIsolation,
		}
		if len(missing) > 0 {
			published.Capabilities = slices.Clone(retained.Capabilities)
		}
		identities = append(identities, published)
	}
	// Validate readable artifacts even when another selected stage is unavailable.
	if len(identities) > 0 {
		identity := identities[0]
		for _, other := range identities[1:] {
			if !MatchingRoutingIdentity(identity, other) {
				return Group{}, "", &RoutingIdentityConflictError{Reason: fmt.Sprintf("ModelService %q has conflicting selected model identities", service.Name)}
			}
			identity.Capabilities = append(identity.Capabilities, other.Capabilities...)
		}
		if len(missing) == 0 || retained != nil {
			return identity, topology, nil
		}
	}
	return Group{}, "", &ModelCatalogProjectionError{Service: service.Name, Reason: fmt.Sprintf("selected %v has no model identity or matching published provenance", missing)}
}

// CatalogTopology describes the execution stages providing a public model.
func CatalogTopology(roles []inferencev1alpha1.ModelRole) string {
	if slices.Contains(roles, inferencev1alpha1.ModelRoleEncoder) {
		return "E/P/D"
	}
	if slices.Contains(roles, inferencev1alpha1.ModelRolePrefill) || slices.Contains(roles, inferencev1alpha1.ModelRoleDecode) {
		return "P/D"
	}
	return "aggregate"
}

type ModelCatalogProjectionError struct{ Service, Reason string }

// Error reports a service-local selected catalog identity that could not be proven.
func (err *ModelCatalogProjectionError) Error() string {
	return fmt.Sprintf("model catalog projection for ModelService %q: %s", err.Service, err.Reason)
}
