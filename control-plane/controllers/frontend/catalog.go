// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves public model identities and admission targets from selected serving cohorts.

package frontend

import (
	"cmp"
	"errors"
	"fmt"
	"reflect"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/vllm"
	vllmomniconfig "github.com/shiweijiezero/foretoken/control-plane/internal/vllmomni"
	"k8s.io/utils/ptr"
)

// projectConfiguredModels resolves model discovery and admission together, independently of capacity.
// Missing selected artifacts exclude only their service unless its published provenance still matches.
func projectConfiguredModels(frontend *inferencev1alpha1.FrontendService, services []inferencev1alpha1.ModelService, pools []inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, previousModels []servingconfig.Model) ([]servingconfig.Model, map[string]servingconfig.Admission, error) {

	models := make([]servingconfig.Model, 0, len(services))
	admission := make(map[string]servingconfig.Admission)
	type contribution struct {
		service, topology string
		identity          servingconfig.Group
	}
	providers := make(map[string]contribution)
	var projectionErr error
	for index := range services {
		service := &services[index]
		if (service.Spec.Backend != "vllm" && service.Spec.Backend != "vllm-omni") || !service.DeletionTimestamp.IsZero() {
			continue
		}
		config, err := effectiveModelAdmission(frontend.Spec, service.Spec)
		if err != nil {
			return nil, nil, fmt.Errorf("ModelService %q admission: %w", service.Name, err)
		}
		servicePools := servingconfig.OwnedRoutingPools(service, pools)
		if config.MaxWaitingRequests != nil {
			var store *inferencev1alpha1.RedisConnection
			if frontend.Status.Application != nil {
				store = frontend.Status.Application.AdmissionStore
			}
			if store == nil {
				return nil, nil, &backendDeploymentRequiredError{service: service.Name}
			}
			// New intent can precede backend rollout. Never publish bounded routes to an older executable.
			for _, pool := range servicePools {
				selected := servingconfig.ServingRevision(service, pool)
				if selected == "" {
					continue
				}
				for index := range groups {
					group := &groups[index]
					if servingconfig.ModelGroupOwnedBy(group, pool) && group.Spec.Revision == selected && !reflect.DeepEqual(group.Spec.Runtime.AdmissionStore, store) {
						return nil, nil, &backendDeploymentRequiredError{service: service.Name}
					}
				}
			}
		}
		compiled, err := compiler.CompileModelService(service.Spec)
		if err != nil {
			return nil, nil, fmt.Errorf("compile ModelService %q catalog: %w", service.Name, err)
		}
		// Pool materialization can lag service intent, including migration from template limits.
		// Keep the previous snapshot until live limits have reached their owning Pool.
		for _, desired := range compiled {
			for _, pool := range servicePools {
				if pool.Spec.PoolName == desired.Name && !ptr.Equal(pool.Spec.MaxInputTokens, desired.MaxInputTokens) {
					return nil, nil, fmt.Errorf("ModelService %q Pool %q request limits are not yet materialized", service.Name, desired.Name)
				}
			}
		}
		var identities []servingconfig.Group
		var roles []inferencev1alpha1.ModelRole
		topology := ""
		if len(service.Status.ServingPoolRevisions) > 0 {
			servicePools = slices.DeleteFunc(servicePools, func(pool *inferencev1alpha1.ModelPool) bool {
				return servingconfig.ServingRevision(service, pool) == ""
			})
			identity, selectedTopology, err := selectedCatalogIdentity(service, pools, groups, previousModels)
			if err != nil {
				var unavailable *modelCatalogProjectionError
				if !errors.As(err, &unavailable) {
					return nil, nil, err
				}
				projectionErr = errors.Join(projectionErr, err)
				continue
			}
			identities = append(identities, identity)
			topology = selectedTopology
		} else {
			// The same compilers used by the service and runtime resolve cold models before Pools or Groups exist.
			for _, pool := range compiled {
				identity, err := configuredTemplateIdentity(pool.Template)
				if err != nil {
					return nil, nil, fmt.Errorf("resolve ModelService %q Pool %q catalog: %w", service.Name, pool.Name, err)
				}
				identities = append(identities, identity)
				roles = append(roles, pool.Template.Role)
			}
		}
		if topology == "" {
			topology = catalogTopology(roles)
		}
		identity := identities[0]
		for _, other := range identities[1:] {
			if !servingconfig.MatchingRoutingIdentity(identity, other) {
				return nil, nil, &servingconfig.RoutingIdentityConflictError{Reason: fmt.Sprintf("ModelService %q has conflicting configured model identities", service.Name)}
			}
			identity.Capabilities = append(identity.Capabilities, other.Capabilities...)
		}
		slices.Sort(identity.Capabilities)
		identity.Capabilities = slices.Compact(identity.Capabilities)
		if previous, exists := providers[identity.Model]; exists {
			if previous.topology != topology || !servingconfig.MatchingRoutingIdentity(previous.identity, identity) || !reflect.DeepEqual(admission[identity.Model], config) {
				return nil, nil, &servingconfig.RoutingIdentityConflictError{Reason: fmt.Sprintf("public model %q has conflicting identity, topology, admission or prefix-cache settings in ModelServices %q and %q", identity.Model, previous.service, service.Name)}
			}
		} else {
			providers[identity.Model] = contribution{service: service.Name, topology: topology, identity: identity}
			admission[identity.Model] = config
		}
		targetSets := admissionTargetSetsForService(service, servicePools)
		if targetSets == nil {
			targetSets = make([][]servingconfig.ScalingTarget, 0)
		}
		models = append(models, servingconfig.Model{
			ServiceGeneration:     service.Generation,
			ServiceUID:            string(service.UID),
			Model:                 identity.Model,
			Source:                identity.Source,
			Revision:              identity.Revision,
			Tokenizer:             identity.Tokenizer,
			TokenizerRevision:     identity.TokenizerRevision,
			PrefixCacheIsolation:  identity.PrefixCacheIsolation,
			RouterPipeline:        service.Spec.RouterPipeline.DeepCopy(),
			SelectedPoolRevisions: servingconfig.SortedServingPoolRevisions(service.Status.ServingPoolRevisions),
			Topology:              topology,
			Capabilities:          identity.Capabilities,
			AdmissionTargetSets:   targetSets,
		})
	}
	slices.SortFunc(models, func(left, right servingconfig.Model) int {
		if compared := cmp.Compare(left.Model, right.Model); compared != 0 {
			return compared
		}
		return cmp.Compare(left.ServiceUID, right.ServiceUID)
	})
	return models, admission, projectionErr
}

// selectedCatalogIdentity reads immutable selected artifacts, including draining Groups.
// Only the same published cohort may supply identity after its artifacts disappear.
func selectedCatalogIdentity(service *inferencev1alpha1.ModelService, pools []inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, previousModels []servingconfig.Model) (servingconfig.Group, string, error) {
	selectedRevisions := servingconfig.SortedServingPoolRevisions(service.Status.ServingPoolRevisions)
	var retained *servingconfig.Model
	for index := range previousModels {
		model := &previousModels[index]
		if model.ServiceUID == string(service.UID) && model.Topology != "" && slices.Equal(model.SelectedPoolRevisions, selectedRevisions) {
			retained = model
			break
		}
	}
	var identities []servingconfig.Group
	var roles []inferencev1alpha1.ModelRole
	var missing []string
	for _, selected := range selectedRevisions {
		found := false
		for poolIndex := range pools {
			pool := &pools[poolIndex]
			if !servingconfig.ModelPoolOwnedBy(pool, service) || string(pool.UID) != selected.PoolUID || pool.Spec.PoolName != selected.PoolName {
				continue
			}
			for groupIndex := range groups {
				group := &groups[groupIndex]
				if !servingconfig.ModelGroupOwnedBy(group, pool) || group.Spec.Revision != selected.Revision {
					continue
				}
				found = true
				identity := servingconfig.RoutingGroupForService(service, pool, group)
				if group.Spec.Role == inferencev1alpha1.ModelRolePrefill || group.Spec.Role == inferencev1alpha1.ModelRoleDecode {
					features := group.Spec.Features
					features.Multimodal = nil
					identity.Capabilities = servingconfig.RoutingCapabilities(features)
				}
				identities = append(identities, identity)
				roles = append(roles, group.Spec.Role)
			}
		}
		if !found {
			missing = append(missing, fmt.Sprintf("Pool %q revision %q", selected.PoolName, selected.Revision))
		}
	}
	topology := catalogTopology(roles)
	if retained != nil {
		// Missing stages keep their published topology; surviving stages must fit it.
		if len(roles) > 0 && topology != retained.Topology && !(len(missing) > 0 && topology == "P/D" && retained.Topology == "E/P/D") {
			return servingconfig.Group{}, "", &servingconfig.RoutingIdentityConflictError{Reason: fmt.Sprintf("ModelService %q selected artifacts conflict with its published topology", service.Name)}
		}
		topology = retained.Topology
		published := servingconfig.Group{
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
			if !servingconfig.MatchingRoutingIdentity(identity, other) {
				return servingconfig.Group{}, "", &servingconfig.RoutingIdentityConflictError{Reason: fmt.Sprintf("ModelService %q has conflicting selected model identities", service.Name)}
			}
			identity.Capabilities = append(identity.Capabilities, other.Capabilities...)
		}
		if len(missing) == 0 || retained != nil {
			return identity, topology, nil
		}
	}
	return servingconfig.Group{}, "", &modelCatalogProjectionError{service: service.Name, reason: fmt.Sprintf("selected %v has no model identity or matching published provenance", missing)}
}

func catalogTopology(roles []inferencev1alpha1.ModelRole) string {
	if slices.Contains(roles, inferencev1alpha1.ModelRoleEncoder) {
		return "E/P/D"
	}
	if slices.Contains(roles, inferencev1alpha1.ModelRolePrefill) || slices.Contains(roles, inferencev1alpha1.ModelRoleDecode) {
		return "P/D"
	}
	return "aggregate"
}

type backendDeploymentRequiredError struct{ service string }

// Error identifies bounded admission that cannot yet share the selected backend ledger.
func (err *backendDeploymentRequiredError) Error() string {
	return fmt.Sprintf("ModelService %q and its frontend must be redeployed with the same admission ledger before bounded admission can be applied", err.service)
}

type modelCatalogProjectionError struct{ service, reason string }

// Error reports a service-local selected catalog identity that could not be proven.
func (err *modelCatalogProjectionError) Error() string {
	return fmt.Sprintf("model catalog projection for ModelService %q: %s", err.service, err.reason)
}

// configuredTemplateIdentity uses the runtime's backend compiler for pre-serving discovery.
func configuredTemplateIdentity(template inferencev1alpha1.NormalizedPoolTemplate) (servingconfig.Group, error) {
	identity := servingconfig.Group{PrefixCacheIsolation: template.PrefixCacheIsolation}
	features := template.Features
	if template.Role == inferencev1alpha1.ModelRolePrefill || template.Role == inferencev1alpha1.ModelRoleDecode {
		features.Multimodal = nil
	}
	identity.Capabilities = servingconfig.RoutingCapabilities(features)
	if template.Backend == "vllm-omni" {
		effective, err := vllmomniconfig.Compile(template)
		if err != nil {
			return servingconfig.Group{}, err
		}
		identity.Model, identity.Source, identity.Revision = effective.Model, effective.Source, effective.Revision
		identity.Tokenizer, identity.TokenizerRevision = effective.Tokenizer, effective.TokenizerRevision
		identity.Capabilities = []string{"video"}
	} else {
		effective, err := vllmconfig.Compile(template)
		if err != nil {
			return servingconfig.Group{}, err
		}
		identity.Model, identity.Source, identity.Revision = effective.Model, effective.Source, effective.Revision
		identity.Tokenizer, identity.TokenizerRevision = effective.Tokenizer, effective.TokenizerRevision
	}
	return identity, nil
}
