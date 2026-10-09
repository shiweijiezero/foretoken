// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Publishes private, versioned ModelGroup discovery snapshots to frontend Pods.

package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/vllm"
	vllmomniconfig "github.com/shiweijiezero/foretoken/control-plane/internal/vllmomni"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// reconcileServingSnapshot publishes the versioned routing and scaling snapshot consumed by frontend Pods.
func (reconciler *FrontendServiceReconciler) reconcileServingSnapshot(ctx context.Context, frontend *inferencev1alpha1.FrontendService, services []inferencev1alpha1.ModelService) (bool, error) {
	name := frontendServingConfigMapName(frontend)
	current := new(corev1.ConfigMap)
	err := reconciler.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: name}, current)
	if err == nil && !metav1.IsControlledBy(current, frontend) {
		return false, fmt.Errorf("ConfigMap %q is not controlled by FrontendService", name)
	}
	if err != nil && !apierrors.IsNotFound(err) {
		return false, fmt.Errorf("get serving snapshot ConfigMap: %w", err)
	}
	var previous servingSnapshot
	previousValid := err == nil && json.Unmarshal([]byte(current.Data[servingSnapshotKey]), &previous) == nil
	if !previousValid {
		previous = servingSnapshot{}
	}
	models, admission, catalogErr := reconciler.projectConfiguredModels(ctx, frontend, services, previous.Models)
	if catalogErr != nil {
		var unavailable *modelCatalogProjectionError
		if !errors.As(catalogErr, &unavailable) {
			return false, catalogErr
		}
	}
	// A service without a proven catalog identity cannot contribute executable routes.
	routingServices := make([]inferencev1alpha1.ModelService, 0, len(models))
	for _, service := range services {
		if slices.ContainsFunc(models, func(model servingSnapshotModel) bool { return model.ServiceUID == string(service.UID) }) {
			routingServices = append(routingServices, service)
		}
	}
	groups, pdComponents, pdPipelineScopes, epdComponents, epdPipelineScopes, projectionErr := reconciler.projectableRouting(ctx, frontend.Namespace, routingServices)
	if projectionErr != nil {
		var splitProjectionError *splitRoutingProjectionError
		if !errors.As(projectionErr, &splitProjectionError) {
			return false, projectionErr
		}
		// Service-local split failures have already been excluded from the partial projection.
	}
	if err := validateRoutingIdentities(models, groups, pdComponents, epdComponents); err != nil {
		return false, err
	}
	projectionErr = errors.Join(catalogErr, projectionErr)

	// Status is the durable version floor, while the persisted ConfigMap is the last semantic payload.
	// Increment only for changed content so recreation or reconcile replay cannot publish an older generation.
	version := frontend.Status.ServingSnapshotVersion
	contentsChanged := true
	if previousValid {
		if previous.Version > version {
			version = previous.Version
		}
		contentsChanged = !reflect.DeepEqual(previous.Admission, admission) || !slices.EqualFunc(previous.Models, models, equalScalingModel) || !slices.EqualFunc(previous.Groups, groups, equalRoutingGroup) || !slices.EqualFunc(previous.PDComponents, pdComponents, equalRoutingPDComponent) || !slices.EqualFunc(previous.PDPipelineScopes, pdPipelineScopes, equalRoutingPDPipelineScope) || !slices.EqualFunc(previous.EPDComponents, epdComponents, equalRoutingEPDComponent) || !slices.EqualFunc(previous.EPDPipelineScopes, epdPipelineScopes, equalRoutingEPDPipelineScope)
	}
	if contentsChanged || version == 0 {
		version++
	}
	payload, err := json.Marshal(servingSnapshot{Version: version, Models: models, Admission: admission, Groups: groups, PDComponents: pdComponents, PDPipelineScopes: pdPipelineScopes, EPDComponents: epdComponents, EPDPipelineScopes: epdPipelineScopes})
	if err != nil {
		return false, fmt.Errorf("encode routing snapshot: %w", err)
	}
	desired := &corev1.ConfigMap{
		TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: frontend.Namespace,
			Labels:    map[string]string{frontendServiceLabel: frontend.Name},
		},
		Data: map[string]string{servingSnapshotKey: string(payload)},
	}
	if err := controllerutil.SetControllerReference(frontend, desired, reconciler.Scheme()); err != nil {
		return false, fmt.Errorf("set serving snapshot ConfigMap owner: %w", err)
	}
	if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(frontendServiceFieldOwner), client.ForceOwnership); err != nil {
		return false, fmt.Errorf("apply serving snapshot ConfigMap: %w", err)
	}
	// Updating Pod metadata prompts kubelet to refresh the projected ConfigMap.
	// Consumer acknowledgements still come from the frontend's active generation.
	const refreshAnnotation = "inference.foretoken.io/serving-config-version"
	refreshVersion := fmt.Sprint(version)
	var pods corev1.PodList
	if err := reconciler.List(ctx, &pods, client.InNamespace(frontend.Namespace), client.MatchingLabels{frontendServiceLabel: frontend.Name}); err != nil {
		return false, fmt.Errorf("list frontend Pods for config refresh: %w", err)
	}
	for index := range pods.Items {
		pod := &pods.Items[index]
		if !pod.DeletionTimestamp.IsZero() || pod.Annotations[refreshAnnotation] == refreshVersion {
			continue
		}
		base := pod.DeepCopy()
		if pod.Annotations == nil {
			pod.Annotations = make(map[string]string)
		}
		pod.Annotations[refreshAnnotation] = refreshVersion
		if err := reconciler.Patch(ctx, pod, client.MergeFrom(base)); err != nil && !apierrors.IsNotFound(err) {
			return false, fmt.Errorf("refresh frontend Pod %q config: %w", pod.Name, err)
		}
	}
	if frontend.Status.ServingSnapshotVersion < version {
		base := frontend.DeepCopy()
		frontend.Status.ServingSnapshotVersion = version
		if err := reconciler.Status().Patch(ctx, frontend, client.MergeFrom(base)); err != nil {
			return false, fmt.Errorf("persist serving snapshot version: %w", err)
		}
	}
	if projectionErr != nil {
		return false, projectionErr
	}
	return len(models) > 0 || len(groups) > 0 || len(pdComponents) > 0 || len(epdComponents) > 0, nil
}

// projectConfiguredModels resolves model discovery and admission together, independently of capacity.
// Missing selected artifacts exclude only their service unless its published provenance still matches.
func (reconciler *FrontendServiceReconciler) projectConfiguredModels(ctx context.Context, frontend *inferencev1alpha1.FrontendService, services []inferencev1alpha1.ModelService, previousModels []servingSnapshotModel) ([]servingSnapshotModel, map[string]inferencev1alpha1.AdmissionConfig, error) {
	var pools inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(frontend.Namespace)); err != nil {
		return nil, nil, fmt.Errorf("list ModelPools for model catalog: %w", err)
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(frontend.Namespace)); err != nil {
		return nil, nil, fmt.Errorf("list ModelGroups for model catalog: %w", err)
	}

	models := make([]servingSnapshotModel, 0, len(services))
	admission := make(map[string]inferencev1alpha1.AdmissionConfig)
	type contribution struct {
		service, topology string
		identity          servingSnapshotGroup
	}
	providers := make(map[string]contribution)
	var projectionErr error
	for index := range services {
		service := &services[index]
		if (service.Spec.Backend != "vllm" && service.Spec.Backend != "vllm-omni") || !service.DeletionTimestamp.IsZero() {
			continue
		}
		servicePools := ownedRoutingPools(service, pools.Items)
		var identities []servingSnapshotGroup
		var roles []inferencev1alpha1.ModelRole
		topology := ""
		if len(service.Status.ServingPoolRevisions) > 0 {
			servicePools = slices.DeleteFunc(servicePools, func(pool *inferencev1alpha1.ModelPool) bool {
				return serviceServingRevision(service, pool) == ""
			})
			identity, selectedTopology, err := selectedCatalogIdentity(service, pools.Items, groups.Items, previousModels)
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
			compiled, err := compiler.CompileModelService(service.Spec)
			if err != nil {
				return nil, nil, fmt.Errorf("compile ModelService %q catalog: %w", service.Name, err)
			}
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
			if !matchingRoutingArtifacts(identity, other) {
				return nil, nil, &routingIdentityConflictError{reason: fmt.Sprintf("ModelService %q has conflicting configured model identities", service.Name)}
			}
			identity.Capabilities = append(identity.Capabilities, other.Capabilities...)
		}
		slices.Sort(identity.Capabilities)
		identity.Capabilities = slices.Compact(identity.Capabilities)
		config, err := effectiveModelAdmission(frontend.Spec.Admission, service.Spec.Admission)
		if err != nil {
			return nil, nil, fmt.Errorf("ModelService %q admission: %w", service.Name, err)
		}
		if previous, exists := providers[identity.Model]; exists {
			if previous.topology != topology || !matchingRoutingArtifacts(previous.identity, identity) || !reflect.DeepEqual(admission[identity.Model], config) {
				return nil, nil, &routingIdentityConflictError{reason: fmt.Sprintf("public model %q has conflicting identity, topology or admission settings in ModelServices %q and %q", identity.Model, previous.service, service.Name)}
			}
		} else {
			providers[identity.Model] = contribution{service: service.Name, topology: topology, identity: identity}
			admission[identity.Model] = config
		}
		targetSets := admissionTargetSetsForService(service, servicePools)
		if targetSets == nil {
			targetSets = make([][]servingSnapshotScalingTarget, 0)
		}
		models = append(models, servingSnapshotModel{
			ServiceUID:            string(service.UID),
			Model:                 identity.Model,
			Source:                identity.Source,
			Revision:              identity.Revision,
			Tokenizer:             identity.Tokenizer,
			TokenizerRevision:     identity.TokenizerRevision,
			SelectedPoolRevisions: sortedServingPoolRevisions(service.Status.ServingPoolRevisions),
			Topology:              topology,
			Capabilities:          identity.Capabilities,
			AdmissionTargetSets:   targetSets,
		})
	}
	slices.SortFunc(models, func(left, right servingSnapshotModel) int {
		if compared := compareStrings(left.Model, right.Model); compared != 0 {
			return compared
		}
		return compareStrings(left.ServiceUID, right.ServiceUID)
	})
	return models, admission, projectionErr
}

// selectedCatalogIdentity reads immutable selected artifacts, including draining Groups.
// Only the same published cohort may supply identity after its artifacts disappear.
func selectedCatalogIdentity(service *inferencev1alpha1.ModelService, pools []inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, previousModels []servingSnapshotModel) (servingSnapshotGroup, string, error) {
	selectedRevisions := sortedServingPoolRevisions(service.Status.ServingPoolRevisions)
	var retained *servingSnapshotModel
	for index := range previousModels {
		model := &previousModels[index]
		if model.ServiceUID == string(service.UID) && model.Topology != "" && slices.Equal(model.SelectedPoolRevisions, selectedRevisions) {
			retained = model
			break
		}
	}
	var identities []servingSnapshotGroup
	var roles []inferencev1alpha1.ModelRole
	var missing []string
	for _, selected := range selectedRevisions {
		found := false
		for poolIndex := range pools {
			pool := &pools[poolIndex]
			if !modelPoolOwnedBy(pool, service) || string(pool.UID) != selected.PoolUID || pool.Spec.PoolName != selected.PoolName {
				continue
			}
			for groupIndex := range groups {
				group := &groups[groupIndex]
				if !modelGroupOwnedBy(group, pool) || group.Spec.Revision != selected.Revision {
					continue
				}
				found = true
				identity := routingGroupForService(service, pool, group)
				if group.Spec.Role == inferencev1alpha1.ModelRolePrefill || group.Spec.Role == inferencev1alpha1.ModelRoleDecode {
					features := group.Spec.Features
					features.Multimodal = nil
					identity.Capabilities = routingCapabilities(features)
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
			return servingSnapshotGroup{}, "", &routingIdentityConflictError{reason: fmt.Sprintf("ModelService %q selected artifacts conflict with its published topology", service.Name)}
		}
		topology = retained.Topology
		published := servingSnapshotGroup{
			Model: retained.Model, Source: retained.Source, Revision: retained.Revision,
			Tokenizer: retained.Tokenizer, TokenizerRevision: retained.TokenizerRevision,
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
			if !matchingRoutingArtifacts(identity, other) {
				return servingSnapshotGroup{}, "", &routingIdentityConflictError{reason: fmt.Sprintf("ModelService %q has conflicting selected model identities", service.Name)}
			}
			identity.Capabilities = append(identity.Capabilities, other.Capabilities...)
		}
		if len(missing) == 0 || retained != nil {
			return identity, topology, nil
		}
	}
	return servingSnapshotGroup{}, "", &modelCatalogProjectionError{service: service.Name, reason: fmt.Sprintf("selected %v has no model identity or matching published provenance", missing)}
}

// sortedServingPoolRevisions makes cohort provenance independent of status list order.
func sortedServingPoolRevisions(revisions []inferencev1alpha1.ServingPoolRevision) []inferencev1alpha1.ServingPoolRevision {
	selected := slices.Clone(revisions)
	slices.SortFunc(selected, func(left, right inferencev1alpha1.ServingPoolRevision) int {
		if compared := compareStrings(left.PoolName, right.PoolName); compared != 0 {
			return compared
		}
		if compared := compareStrings(left.PoolUID, right.PoolUID); compared != 0 {
			return compared
		}
		return compareStrings(left.Revision, right.Revision)
	})
	return selected
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

type modelCatalogProjectionError struct{ service, reason string }

// Error reports a service-local selected catalog identity that could not be proven.
func (err *modelCatalogProjectionError) Error() string {
	return fmt.Sprintf("model catalog projection for ModelService %q: %s", err.service, err.reason)
}

// configuredTemplateIdentity uses the runtime's backend compiler for pre-serving discovery.
func configuredTemplateIdentity(template inferencev1alpha1.NormalizedPoolTemplate) (servingSnapshotGroup, error) {
	identity := servingSnapshotGroup{}
	features := template.Features
	if template.Role == inferencev1alpha1.ModelRolePrefill || template.Role == inferencev1alpha1.ModelRoleDecode {
		features.Multimodal = nil
	}
	identity.Capabilities = routingCapabilities(features)
	if template.Backend == "vllm-omni" {
		effective, err := vllmomniconfig.Compile(template)
		if err != nil {
			return servingSnapshotGroup{}, err
		}
		identity.Model, identity.Source, identity.Revision = effective.Model, effective.Source, effective.Revision
		identity.Tokenizer, identity.TokenizerRevision = effective.Tokenizer, effective.TokenizerRevision
		identity.Capabilities = []string{"video"}
	} else {
		effective, err := vllmconfig.Compile(template)
		if err != nil {
			return servingSnapshotGroup{}, err
		}
		identity.Model, identity.Source, identity.Revision = effective.Model, effective.Source, effective.Revision
		identity.Tokenizer, identity.TokenizerRevision = effective.Tokenizer, effective.TokenizerRevision
	}
	return identity, nil
}

// effectiveModelAdmission resolves whole-block overrides into stable snapshot semantics.
func effectiveModelAdmission(defaults, override *inferencev1alpha1.AdmissionConfig) (inferencev1alpha1.AdmissionConfig, error) {
	selected := override
	if selected == nil {
		selected = defaults
	}
	config := inferencev1alpha1.AdmissionConfig{Algorithm: "allow_all"}
	if selected != nil {
		config = *selected.DeepCopy()
		if config.Algorithm == "" {
			config.Algorithm = "allow_all"
		}
	}
	if parameters := config.Parameters; parameters != nil && parameters.QueueTimeout != "" {
		timeout, err := time.ParseDuration(string(parameters.QueueTimeout))
		if err != nil {
			return inferencev1alpha1.AdmissionConfig{}, err
		}
		parameters.QueueTimeout = inferencev1alpha1.Duration(timeout.String())
	}
	return config, nil
}

// admissionTargetSetsForService selects autoscaling targets that admit each service request.
func admissionTargetSetsForService(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool) [][]servingSnapshotScalingTarget {
	targets := make([]servingSnapshotScalingTarget, 0, len(pools))
	for _, pool := range pools {
		role := pool.Spec.Template.Role
		if role != inferencev1alpha1.ModelRoleAggregate && role != inferencev1alpha1.ModelRoleEncoder && role != inferencev1alpha1.ModelRolePrefill && role != inferencev1alpha1.ModelRoleDecode {
			continue
		}
		targets = append(targets, servingSnapshotScalingTarget{ServiceUID: string(service.UID), Name: pool.Spec.PoolName, UID: string(pool.UID), Kind: string(core.TargetPool)})
	}
	slices.SortFunc(targets, func(left, right servingSnapshotScalingTarget) int {
		return compareStrings(left.UID, right.UID)
	})
	if poolsHaveEPD(pools) {
		if !poolsContainCompleteEPD(pools) {
			return nil
		}
		return [][]servingSnapshotScalingTarget{targets}
	}
	if poolsHavePD(pools) || len(pools) == 0 && serviceDeclaresPD(service) {
		hasPrefill := slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
			return pool.Spec.Template.Role == inferencev1alpha1.ModelRolePrefill
		})
		hasDecode := slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
			return pool.Spec.Template.Role == inferencev1alpha1.ModelRoleDecode
		})
		if !hasPrefill || !hasDecode {
			return nil
		}
		return [][]servingSnapshotScalingTarget{targets}
	}
	sets := make([][]servingSnapshotScalingTarget, len(targets))
	for index, target := range targets {
		sets[index] = []servingSnapshotScalingTarget{target}
	}
	return sets
}

// projectableRouting follows only the Ready Service -> owned/Ready Pool -> owned/Ready
// Group chain. A Service declaring a P/D Pool never contributes aggregate routes.
func (reconciler *FrontendServiceReconciler) projectableRouting(ctx context.Context, namespace string, services []inferencev1alpha1.ModelService) ([]servingSnapshotGroup, []servingSnapshotPDComponent, []servingSnapshotPDPipelineScope, []servingSnapshotEPDComponent, []servingSnapshotEPDPipelineScope, error) {
	var pools inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(namespace)); err != nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("list ModelPools for routing: %w", err)
	}
	var modelGroups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &modelGroups, client.InNamespace(namespace)); err != nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("list ModelGroups for routing: %w", err)
	}

	groups := make([]servingSnapshotGroup, 0, len(modelGroups.Items))
	pdComponents := make([]servingSnapshotPDComponent, 0)
	pdPipelineScopes := make([]servingSnapshotPDPipelineScope, 0)
	epdComponents := make([]servingSnapshotEPDComponent, 0)
	epdPipelineScopes := make([]servingSnapshotEPDPipelineScope, 0)
	var projectionErr error
	for serviceIndex := range services {
		service := &services[serviceIndex]
		if (service.Spec.Backend != "vllm" && service.Spec.Backend != "vllm-omni") || !modelServiceReady(service) {
			continue
		}
		servicePools := ownedRoutingPools(service, pools.Items)
		servicePools = slices.DeleteFunc(servicePools, func(pool *inferencev1alpha1.ModelPool) bool {
			return serviceServingRevision(service, pool) == ""
		})
		if poolsHaveEPD(servicePools) {
			components, pipelineScopes, err := projectServiceEPDComponents(service, servicePools, modelGroups.Items)
			if err != nil {
				// An incomplete E/P/D Service must not withdraw other Services' routes.
				projectionErr = errors.Join(projectionErr, err)
				continue
			}
			epdComponents = append(epdComponents, components...)
			epdPipelineScopes = append(epdPipelineScopes, pipelineScopes...)
			continue
		}
		if poolsHavePD(servicePools) {
			components, pipelineScope, err := projectServicePDComponents(service, servicePools, modelGroups.Items)
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
			if serviceServingRevision(service, pool) == "" {
				continue
			}
			for groupIndex := range modelGroups.Items {
				group := &modelGroups.Items[groupIndex]
				if !routingGroupOwnedBy(group, pool) || group.Spec.Revision != serviceServingRevision(service, pool) || !routingGroupReady(group) || group.Spec.Role != inferencev1alpha1.ModelRoleAggregate {
					continue
				}
				groups = append(groups, routingGroupForService(service, pool, group))
			}
		}
	}
	slices.SortFunc(groups, compareRoutingGroups)
	slices.SortFunc(pdComponents, compareRoutingPDComponents)
	slices.SortFunc(pdPipelineScopes, compareRoutingPDPipelineScopes)
	slices.SortFunc(epdComponents, compareRoutingEPDComponents)
	slices.SortFunc(epdPipelineScopes, compareRoutingEPDPipelineScopes)
	if err := validateRoutingIdentities(nil, groups, pdComponents, epdComponents); err != nil {
		return nil, nil, nil, nil, nil, err
	}
	return groups, pdComponents, pdPipelineScopes, epdComponents, epdPipelineScopes, projectionErr
}

func ownedRoutingPools(service *inferencev1alpha1.ModelService, pools []inferencev1alpha1.ModelPool) []*inferencev1alpha1.ModelPool {
	owned := make([]*inferencev1alpha1.ModelPool, 0)
	for index := range pools {
		if routingPoolOwnedBy(&pools[index], service) {
			owned = append(owned, &pools[index])
		}
	}
	return owned
}

func routingPoolName(pools []*inferencev1alpha1.ModelPool, group *inferencev1alpha1.ModelGroup) string {
	for _, pool := range pools {
		if string(pool.UID) == group.Spec.ModelPoolRef.UID {
			return pool.Spec.PoolName
		}
	}
	return ""
}

func serviceDeclaresEPD(service *inferencev1alpha1.ModelService) bool {
	return slices.ContainsFunc(service.Spec.ModelPools, func(pool inferencev1alpha1.ModelPoolTemplate) bool {
		return pool.Role == inferencev1alpha1.ModelRoleEncoder
	})
}

func serviceDeclaresPD(service *inferencev1alpha1.ModelService) bool {
	return slices.ContainsFunc(service.Spec.ModelPools, func(pool inferencev1alpha1.ModelPoolTemplate) bool {
		return pool.Role == inferencev1alpha1.ModelRolePrefill || pool.Role == inferencev1alpha1.ModelRoleDecode
	})
}

func poolsHaveEPD(pools []*inferencev1alpha1.ModelPool) bool {
	return slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
		return pool.Spec.Template.Role == inferencev1alpha1.ModelRoleEncoder
	})
}

func poolsContainCompleteEPD(pools []*inferencev1alpha1.ModelPool) bool {
	for _, role := range []inferencev1alpha1.ModelRole{inferencev1alpha1.ModelRoleEncoder, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode} {
		if !slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
			return pool.Spec.Template.Role == role
		}) {
			return false
		}
	}
	return true
}

func poolsHavePD(pools []*inferencev1alpha1.ModelPool) bool {
	return slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
		return pool.Spec.Template.Role == inferencev1alpha1.ModelRolePrefill || pool.Spec.Template.Role == inferencev1alpha1.ModelRoleDecode
	})
}

// projectServicePDComponents publishes compatible ready prefill and decode components for one service.
func projectServicePDComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]servingSnapshotPDComponent, servingSnapshotPDPipelineScope, error) {
	var prefills, decodes []*inferencev1alpha1.ModelGroup
	for _, pool := range pools {
		if serviceServingRevision(service, pool) == "" {
			continue
		}
		role := pool.Spec.Template.Role
		if role != inferencev1alpha1.ModelRolePrefill && role != inferencev1alpha1.ModelRoleDecode {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if routingGroupOwnedBy(group, pool) && group.Spec.Revision == serviceServingRevision(service, pool) && routingGroupReady(group) && group.Spec.Role == role {
				if !completePDRuntime(group.Spec.PDRuntime) {
					return nil, servingSnapshotPDPipelineScope{}, &splitRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("Ready %s ModelGroup %q has an incomplete pdRuntime", role, group.Name)}
				}
				if role == inferencev1alpha1.ModelRolePrefill {
					prefills = append(prefills, group)
				} else {
					decodes = append(decodes, group)
				}
			}
		}
	}
	if len(prefills) == 0 || len(decodes) == 0 {
		return nil, servingSnapshotPDPipelineScope{}, &splitRoutingProjectionError{service: service.Name, reason: "requires at least one Ready prefill ModelGroup and one Ready decode ModelGroup"}
	}
	for _, group := range append(prefills, decodes...) {
		if !compatiblePDGroups(prefills[0], group) {
			return nil, servingSnapshotPDPipelineScope{}, &splitRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("Ready P/D ModelGroup %q conflicts with the Service P/D identity", group.Name)}
		}
	}
	pipelineScopeID := "pd:" + string(service.UID)
	components := make([]servingSnapshotPDComponent, 0, len(prefills)+len(decodes))
	pipelineScope := servingSnapshotPDPipelineScope{PipelineScopeID: pipelineScopeID}
	for _, group := range prefills {
		components = append(components, routingPDComponent(service, group, routingPoolName(pools, group), pipelineScopeID))
		pipelineScope.PrefillRouteTargetIDs = append(pipelineScope.PrefillRouteTargetIDs, string(group.UID))
	}
	for _, group := range decodes {
		components = append(components, routingPDComponent(service, group, routingPoolName(pools, group), pipelineScopeID))
		pipelineScope.DecodeRouteTargetIDs = append(pipelineScope.DecodeRouteTargetIDs, string(group.UID))
	}
	slices.Sort(pipelineScope.PrefillRouteTargetIDs)
	slices.Sort(pipelineScope.DecodeRouteTargetIDs)
	return components, pipelineScope, nil
}

// projectServiceEPDComponents publishes every Ready route in one service-local compatibility scope.
func projectServiceEPDComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]servingSnapshotEPDComponent, []servingSnapshotEPDPipelineScope, error) {
	type epdScope struct {
		encoders []*inferencev1alpha1.ModelGroup
		prefills []*inferencev1alpha1.ModelGroup
		decodes  []*inferencev1alpha1.ModelGroup
	}
	byRole := map[inferencev1alpha1.ModelRole][]*inferencev1alpha1.ModelGroup{
		inferencev1alpha1.ModelRoleEncoder: {}, inferencev1alpha1.ModelRolePrefill: {}, inferencev1alpha1.ModelRoleDecode: {},
	}
	for _, pool := range pools {
		if serviceServingRevision(service, pool) == "" {
			continue
		}
		role := pool.Spec.Template.Role
		if _, selected := byRole[role]; !selected {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if routingGroupOwnedBy(group, pool) && group.Spec.Revision == serviceServingRevision(service, pool) && routingGroupReady(group) && group.Spec.Role == role {
				byRole[role] = append(byRole[role], group)
			}
		}
	}
	encoders, prefills, decodes := byRole[inferencev1alpha1.ModelRoleEncoder], byRole[inferencev1alpha1.ModelRolePrefill], byRole[inferencev1alpha1.ModelRoleDecode]
	if len(encoders) == 0 || len(prefills) == 0 || len(decodes) == 0 {
		return nil, nil, &splitRoutingProjectionError{service: service.Name, reason: "requires at least one Ready encoder, prefill, and decode ModelGroup"}
	}
	slices.SortFunc(encoders, func(left, right *inferencev1alpha1.ModelGroup) int {
		return compareStrings(string(left.UID), string(right.UID))
	})
	slices.SortFunc(prefills, func(left, right *inferencev1alpha1.ModelGroup) int {
		return compareStrings(string(left.UID), string(right.UID))
	})
	slices.SortFunc(decodes, func(left, right *inferencev1alpha1.ModelGroup) int {
		return compareStrings(string(left.UID), string(right.UID))
	})
	var scopes []epdScope
	remainingEncoders := append([]*inferencev1alpha1.ModelGroup(nil), encoders...)
	remainingPrefills := append([]*inferencev1alpha1.ModelGroup(nil), prefills...)
	remainingDecodes := append([]*inferencev1alpha1.ModelGroup(nil), decodes...)
	for len(remainingEncoders) > 0 || len(remainingPrefills) > 0 || len(remainingDecodes) > 0 {
		if len(remainingEncoders) == 0 || len(remainingPrefills) == 0 || len(remainingDecodes) == 0 {
			return nil, nil, &splitRoutingProjectionError{service: service.Name, reason: "ready E/P/D groups cannot be partitioned into complete compatible scopes"}
		}
		seedEncoder := remainingEncoders[0]
		seedPrefillIndex := slices.IndexFunc(remainingPrefills, func(candidate *inferencev1alpha1.ModelGroup) bool {
			return compatibleEncoderPrefill(seedEncoder, candidate)
		})
		if seedPrefillIndex < 0 {
			return nil, nil, &splitRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("encoder ModelGroup %q has no compatible prefill scope", seedEncoder.Name)}
		}
		seedPrefill := remainingPrefills[seedPrefillIndex]
		seedDecodeIndex := slices.IndexFunc(remainingDecodes, func(candidate *inferencev1alpha1.ModelGroup) bool {
			return compatiblePDGroups(seedPrefill, candidate)
		})
		if seedDecodeIndex < 0 {
			return nil, nil, &splitRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("prefill ModelGroup %q has no compatible decode scope", seedPrefill.Name)}
		}
		seedDecode := remainingDecodes[seedDecodeIndex]
		scope := epdScope{
			encoders: []*inferencev1alpha1.ModelGroup{seedEncoder},
			prefills: []*inferencev1alpha1.ModelGroup{seedPrefill},
			decodes:  []*inferencev1alpha1.ModelGroup{seedDecode},
		}
		remainingEncoders = remainingEncoders[1:]
		remainingPrefills = slices.Delete(remainingPrefills, seedPrefillIndex, seedPrefillIndex+1)
		remainingDecodes = slices.Delete(remainingDecodes, seedDecodeIndex, seedDecodeIndex+1)
		for index := 0; index < len(remainingEncoders); {
			candidate := remainingEncoders[index]
			if compatibleEncoderPrefill(candidate, seedPrefill) {
				scope.encoders = append(scope.encoders, candidate)
				remainingEncoders = slices.Delete(remainingEncoders, index, index+1)
			} else {
				index++
			}
		}
		for index := 0; index < len(remainingPrefills); {
			candidate := remainingPrefills[index]
			if compatibleEncoderPrefill(seedEncoder, candidate) && compatiblePDGroups(candidate, seedDecode) {
				scope.prefills = append(scope.prefills, candidate)
				remainingPrefills = slices.Delete(remainingPrefills, index, index+1)
			} else {
				index++
			}
		}
		for index := 0; index < len(remainingDecodes); {
			candidate := remainingDecodes[index]
			if compatiblePDGroups(seedPrefill, candidate) {
				scope.decodes = append(scope.decodes, candidate)
				remainingDecodes = slices.Delete(remainingDecodes, index, index+1)
			} else {
				index++
			}
		}
		scopes = append(scopes, scope)
	}
	var components []servingSnapshotEPDComponent
	var pipelineScopes []servingSnapshotEPDPipelineScope
	for index, scope := range scopes {
		if len(scope.encoders) == 0 || len(scope.prefills) == 0 || len(scope.decodes) == 0 {
			continue
		}
		pipelineScopeID := fmt.Sprintf("epd:%s:%d", service.UID, index)
		pipelineScope := servingSnapshotEPDPipelineScope{PipelineScopeID: pipelineScopeID}
		for _, group := range scope.encoders {
			components = append(components, routingEPDComponent(service, group, routingPoolName(pools, group)))
			pipelineScope.EncoderRouteTargetIDs = append(pipelineScope.EncoderRouteTargetIDs, string(group.UID))
		}
		for _, group := range scope.prefills {
			components = append(components, routingEPDComponent(service, group, routingPoolName(pools, group)))
			pipelineScope.PrefillRouteTargetIDs = append(pipelineScope.PrefillRouteTargetIDs, string(group.UID))
		}
		for _, group := range scope.decodes {
			components = append(components, routingEPDComponent(service, group, routingPoolName(pools, group)))
			pipelineScope.DecodeRouteTargetIDs = append(pipelineScope.DecodeRouteTargetIDs, string(group.UID))
		}
		slices.Sort(pipelineScope.EncoderRouteTargetIDs)
		slices.Sort(pipelineScope.PrefillRouteTargetIDs)
		slices.Sort(pipelineScope.DecodeRouteTargetIDs)
		pipelineScopes = append(pipelineScopes, pipelineScope)
	}
	if len(pipelineScopes) == 0 {
		return nil, nil, &splitRoutingProjectionError{service: service.Name, reason: "no compatible Ready encoder, prefill, and decode scope"}
	}
	return components, pipelineScopes, nil
}

func compatibleEncoderPrefill(encoder, prefill *inferencev1alpha1.ModelGroup) bool {
	return matchingRoutingArtifacts(routingGroup(encoder), routingGroup(prefill)) &&
		completeECRuntime(encoder.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleProducer) && completeECRuntime(prefill.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleConsumer) &&
		matchingECRuntime(encoder.Spec.ECRuntime, prefill.Spec.ECRuntime)
}

func matchingECRuntime(left, right *inferencev1alpha1.ModelGroupECRuntimeConfig) bool {
	return left.ServiceUID == right.ServiceUID && left.Generation == right.Generation && left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.SharedStorageClaim == right.SharedStorageClaim && left.SharedStoragePath == right.SharedStoragePath
}

func routingParallelism(group *inferencev1alpha1.ModelGroup) servingSnapshotParallelism {
	return servingSnapshotParallelism{
		TP:  group.Spec.Parallelism.TP,
		PP:  group.Spec.Parallelism.PP,
		DP:  group.Spec.Parallelism.DP,
		PCP: group.Spec.Parallelism.PCP,
		DCP: group.Spec.Parallelism.DCP,
		EP:  group.Spec.Parallelism.EP != nil,
	}
}

func routingEPDComponent(service *inferencev1alpha1.ModelService, group *inferencev1alpha1.ModelGroup, poolName string) servingSnapshotEPDComponent {
	features := group.Spec.Features
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill || group.Spec.Role == inferencev1alpha1.ModelRoleDecode {
		features.Multimodal = nil
	}
	component := servingSnapshotEPDComponent{
		RouteTargetID:     string(group.UID),
		ServiceUID:        string(service.UID),
		PoolUID:           group.Spec.ModelPoolRef.UID,
		PoolName:          poolName,
		Role:              string(group.Spec.Role),
		Model:             group.Spec.Artifacts.Model,
		Source:            group.Spec.Artifacts.Source,
		Revision:          group.Spec.Artifacts.ModelRevision,
		Tokenizer:         group.Spec.Artifacts.Tokenizer,
		TokenizerRevision: group.Spec.Artifacts.TokenizerRevision,
		MaxInputTokens:    copyOptionalInt32(group.Spec.MaxInputTokens),
		Capabilities:      routingCapabilities(features),
		Endpoint:          modelGroupEndpoint(group, group.Spec.Runtime.Port),
		KVScopeID:         kvScopeID(group),
		KVLookupScope:     sharedKVLookupScope(group),
		DataParallelSize:  group.Spec.Parallelism.DP,
		Parallelism:       routingParallelism(group),
	}
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
		component.PrefillBootstrapEndpoint = modelGroupEndpoint(group, group.Spec.PDRuntime.BootstrapPort)
	}
	return component
}

func modelServiceReady(service *inferencev1alpha1.ModelService) bool {
	return service != nil && service.DeletionTimestamp.IsZero() && len(service.Status.ServingPoolRevisions) > 0
}

func routingPoolOwnedBy(pool *inferencev1alpha1.ModelPool, service *inferencev1alpha1.ModelService) bool {
	return modelPoolOwnedBy(pool, service) && pool.DeletionTimestamp.IsZero()
}

// modelPoolOwnedBy verifies identity ownership independently of routing eligibility.
func modelPoolOwnedBy(pool *inferencev1alpha1.ModelPool, service *inferencev1alpha1.ModelService) bool {
	return pool != nil && service != nil &&
		pool.Spec.ModelServiceRef.Name == service.Name && pool.Spec.ModelServiceRef.UID == string(service.UID) &&
		routingControllerOwnerMatches(pool, inferencev1alpha1.GroupVersion.String(), "ModelService", service.Name, service.UID)
}

func routingGroupOwnedBy(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) bool {
	return modelGroupOwnedBy(group, pool) && group.DeletionTimestamp.IsZero()
}

// modelGroupOwnedBy verifies immutable artifact ownership even during deletion drain.
func modelGroupOwnedBy(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) bool {
	return group != nil && pool != nil &&
		group.Spec.ModelPoolRef.Name == pool.Name && group.Spec.ModelPoolRef.UID == string(pool.UID) &&
		routingControllerOwnerMatches(group, inferencev1alpha1.GroupVersion.String(), "ModelPool", pool.Name, pool.UID)
}

func routingControllerOwnerMatches(object metav1.Object, apiVersion, kind, name string, uid types.UID) bool {
	for _, owner := range object.GetOwnerReferences() {
		if owner.Controller != nil && *owner.Controller && owner.APIVersion == apiVersion && owner.Kind == kind && owner.Name == name && owner.UID == uid {
			return true
		}
	}
	return false
}

func routingGroupReady(group *inferencev1alpha1.ModelGroup) bool {
	if !group.DeletionTimestamp.IsZero() {
		return false
	}
	ready := meta.FindStatusCondition(group.Status.Conditions, conditionReady)
	return ready != nil && ready.Status == metav1.ConditionTrue && ready.ObservedGeneration == group.Generation && group.Status.ReadyMembers == group.Spec.MemberCount
}

type routingIdentityConflictError struct{ reason string }

// Error describes a snapshot-wide public-model identity conflict.
func (err *routingIdentityConflictError) Error() string {
	return "routing projection: " + err.reason
}

type splitRoutingProjectionError struct{ service, reason string }

// Error describes a service-local P/D or E/P/D routing projection failure.
func (err *splitRoutingProjectionError) Error() string {
	if err.service == "" {
		return "split routing projection: " + err.reason
	}
	return fmt.Sprintf("split routing projection for ModelService %q: %s", err.service, err.reason)
}

func routingGroup(group *inferencev1alpha1.ModelGroup) servingSnapshotGroup {
	return servingSnapshotGroup{
		RouteTargetID:     string(group.UID),
		Model:             group.Spec.Artifacts.Model,
		Source:            group.Spec.Artifacts.Source,
		Revision:          group.Spec.Artifacts.ModelRevision,
		Tokenizer:         group.Spec.Artifacts.Tokenizer,
		TokenizerRevision: group.Spec.Artifacts.TokenizerRevision,
		MaxInputTokens:    copyOptionalInt32(group.Spec.MaxInputTokens),
		Capabilities:      routingCapabilities(group.Spec.Features),
		Endpoint:          modelGroupEndpoint(group, group.Spec.Runtime.Port),
		KVScopeID:         kvScopeID(group),
		KVLookupScope:     sharedKVLookupScope(group),
		DataParallelSize:  group.Spec.Parallelism.DP,
	}
}
func routingGroupForService(service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool, group *inferencev1alpha1.ModelGroup) servingSnapshotGroup {
	route := routingGroup(group)
	if group.Spec.Runtime.Backend == "vllm-omni" {
		route.Capabilities = []string{"video"}
	}
	route.ServiceUID, route.PoolUID, route.PoolName = string(service.UID), group.Spec.ModelPoolRef.UID, pool.Spec.PoolName
	return route
}

func routingCapabilities(features inferencev1alpha1.ModelFeatures) []string {
	capabilities := []string{"chat", "text"}
	optional := make(map[string]struct{})
	if features.Tools {
		optional["tool_calling"] = struct{}{}
	}
	if features.Reasoning {
		optional["reasoning"] = struct{}{}
	}
	for _, format := range features.StructuredOutputs {
		switch format {
		case inferencev1alpha1.StructuredOutputFormatJSONObject:
			optional["structured_output.json_object"] = struct{}{}
		case inferencev1alpha1.StructuredOutputFormatJSONSchema:
			optional["structured_output.json_schema"] = struct{}{}
		case inferencev1alpha1.StructuredOutputFormatStructuralTag:
			optional["structured_output.structural_tag"] = struct{}{}
		}
	}
	for _, modality := range features.Multimodal {
		if modality == inferencev1alpha1.MultimodalModalityImage {
			optional["multimodal.image"] = struct{}{}
		}
	}
	if len(features.Multimodal) > 0 {
		optional["multimodal"] = struct{}{}
	}
	for capability := range optional {
		capabilities = append(capabilities, capability)
	}
	slices.Sort(capabilities[2:])
	return capabilities
}

func compatiblePDGroups(prefill, decode *inferencev1alpha1.ModelGroup) bool {
	return matchingRoutingArtifacts(routingGroup(prefill), routingGroup(decode)) && vllmconfig.CompatibleKVTransfer(prefill.Spec, decode.Spec) && matchingPDRuntime(prefill.Spec.PDRuntime, decode.Spec.PDRuntime)
}

func completePDRuntime(runtime *inferencev1alpha1.ModelGroupPDRuntimeConfig) bool {
	return runtime != nil && runtime.ProfileName != "" && runtime.ProfileRevision != "" && runtime.Connector == "MooncakeConnector" && (runtime.Protocol == "rdma" || runtime.Protocol == "tcp") && runtime.BootstrapPort > 0 && runtime.AbortRequestTimeoutSeconds > 0
}

func matchingPDRuntime(left, right *inferencev1alpha1.ModelGroupPDRuntimeConfig) bool {
	return completePDRuntime(left) && completePDRuntime(right) && left.ServiceUID == right.ServiceUID && left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.Protocol == right.Protocol && left.BootstrapPort == right.BootstrapPort && left.AbortRequestTimeoutSeconds == right.AbortRequestTimeoutSeconds && left.RDMADeviceName == right.RDMADeviceName
}

func routingPDComponent(service *inferencev1alpha1.ModelService, group *inferencev1alpha1.ModelGroup, poolName, pipelineScopeID string) servingSnapshotPDComponent {
	pd := group.Spec.PDRuntime
	features := group.Spec.Features
	// No P/D runtime profile currently verifies multimodal support. Never publish
	// it from P/D routes, including objects created before API validation existed.
	features.Multimodal = nil
	component := servingSnapshotPDComponent{
		RouteTargetID:     string(group.UID),
		ServiceUID:        string(service.UID),
		PoolUID:           group.Spec.ModelPoolRef.UID,
		PoolName:          poolName,
		Role:              string(group.Spec.Role),
		PipelineScopeID:   pipelineScopeID,
		Model:             group.Spec.Artifacts.Model,
		Source:            group.Spec.Artifacts.Source,
		Revision:          group.Spec.Artifacts.ModelRevision,
		Tokenizer:         group.Spec.Artifacts.Tokenizer,
		TokenizerRevision: group.Spec.Artifacts.TokenizerRevision,
		MaxInputTokens:    copyOptionalInt32(group.Spec.MaxInputTokens),
		ProfileName:       pd.ProfileName,
		ProfileRevision:   pd.ProfileRevision,
		Connector:         pd.Connector,
		Protocol:          pd.Protocol,
		Capabilities:      routingCapabilities(features),
		Endpoint:          modelGroupEndpoint(group, group.Spec.Runtime.Port),
		KVScopeID:         kvScopeID(group),
		KVLookupScope:     sharedKVLookupScope(group),
		DataParallelSize:  group.Spec.Parallelism.DP,
		Parallelism:       routingParallelism(group),
	}
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
		component.PrefillBootstrapEndpoint = modelGroupEndpoint(group, pd.BootstrapPort)
	}
	return component
}

func modelGroupEndpoint(group *inferencev1alpha1.ModelGroup, port int32) string {
	return fmt.Sprintf("http://%s.%s.svc:%d", modelGroupServiceName(group), group.Namespace, port)
}

// validateRoutingIdentities rejects topology overlap and conflicting public-model identities before snapshot publication.
func validateRoutingIdentities(models []servingSnapshotModel, groups []servingSnapshotGroup, components []servingSnapshotPDComponent, epdComponents []servingSnapshotEPDComponent) error {
	// One public model must have unambiguous stage semantics and one model/tokenizer identity.
	// Connector compatibility remains local to each P/D or E/P/D scope.
	type identity struct {
		topology, routeTargetID, revision, tokenizer, tokenizerRevision string
		source                                                          inferencev1alpha1.ModelSource
	}
	byModel := make(map[string]identity)
	add := func(model string, current identity) error {
		previous, exists := byModel[model]
		if !exists {
			byModel[model] = current
			return nil
		}
		if previous.topology != "" && current.topology != "" && previous.topology != current.topology {
			return &routingIdentityConflictError{reason: fmt.Sprintf("public model %q is provided by both %s and %s routes", model, previous.topology, current.topology)}
		}
		if previous.source != current.source || previous.revision != current.revision || previous.tokenizer != current.tokenizer || previous.tokenizerRevision != current.tokenizerRevision {
			return &routingIdentityConflictError{reason: fmt.Sprintf("public model %q has conflicting %s route identities %q and %q", model, current.topology, previous.routeTargetID, current.routeTargetID)}
		}
		if previous.topology == "" && current.topology != "" {
			byModel[model] = current
		}
		return nil
	}
	for _, model := range models {
		if err := add(model.Model, identity{topology: model.Topology, routeTargetID: model.ServiceUID, source: model.Source, revision: model.Revision, tokenizer: model.Tokenizer, tokenizerRevision: model.TokenizerRevision}); err != nil {
			return err
		}
	}
	for _, group := range groups {
		if err := add(group.Model, identity{topology: "aggregate", routeTargetID: group.RouteTargetID, source: group.Source, revision: group.Revision, tokenizer: group.Tokenizer, tokenizerRevision: group.TokenizerRevision}); err != nil {
			return err
		}
	}
	for _, component := range components {
		if err := add(component.Model, identity{topology: "P/D", routeTargetID: component.RouteTargetID, source: component.Source, revision: component.Revision, tokenizer: component.Tokenizer, tokenizerRevision: component.TokenizerRevision}); err != nil {
			return err
		}
	}
	for _, component := range epdComponents {
		if err := add(component.Model, identity{topology: "E/P/D", routeTargetID: component.RouteTargetID, source: component.Source, revision: component.Revision, tokenizer: component.Tokenizer, tokenizerRevision: component.TokenizerRevision}); err != nil {
			return err
		}
	}
	return nil
}
func matchingRoutingArtifacts(left, right servingSnapshotGroup) bool {
	return left.Model == right.Model && left.Source == right.Source && left.Revision == right.Revision && left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision
}
func equalScalingModel(left, right servingSnapshotModel) bool {
	return left.ServiceUID == right.ServiceUID && left.Model == right.Model && left.Source == right.Source && left.Revision == right.Revision && left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision && left.Topology == right.Topology && slices.Equal(left.SelectedPoolRevisions, right.SelectedPoolRevisions) && slices.Equal(left.Capabilities, right.Capabilities) && slices.EqualFunc(left.AdmissionTargetSets, right.AdmissionTargetSets, func(left, right []servingSnapshotScalingTarget) bool { return slices.Equal(left, right) })
}

func equalRoutingGroup(left, right servingSnapshotGroup) bool {
	return left.RouteTargetID == right.RouteTargetID && left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName && matchingRoutingArtifacts(left, right) && equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && left.Endpoint == right.Endpoint && left.KVScopeID == right.KVScopeID && left.KVLookupScope == right.KVLookupScope && left.DataParallelSize == right.DataParallelSize && slices.Equal(left.Capabilities, right.Capabilities)
}
func equalRoutingPDComponent(left, right servingSnapshotPDComponent) bool {
	return left.RouteTargetID == right.RouteTargetID &&
		left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName &&
		left.Role == right.Role && left.PipelineScopeID == right.PipelineScopeID &&
		left.Model == right.Model && left.Source == right.Source && left.Revision == right.Revision &&
		left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision &&
		equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) &&
		left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision &&
		left.Connector == right.Connector && left.Protocol == right.Protocol &&
		left.Endpoint == right.Endpoint && left.PrefillBootstrapEndpoint == right.PrefillBootstrapEndpoint &&
		left.KVScopeID == right.KVScopeID && left.KVLookupScope == right.KVLookupScope &&
		left.DataParallelSize == right.DataParallelSize && slices.Equal(left.Capabilities, right.Capabilities)
}

func copyOptionalInt32(value *int32) *int32 {
	if value == nil {
		return nil
	}
	copied := *value
	return &copied
}

func equalOptionalInt32(left, right *int32) bool {
	return left == nil && right == nil || left != nil && right != nil && *left == *right
}
func equalRoutingPDPipelineScope(left, right servingSnapshotPDPipelineScope) bool {
	return left.PipelineScopeID == right.PipelineScopeID && slices.Equal(left.PrefillRouteTargetIDs, right.PrefillRouteTargetIDs) && slices.Equal(left.DecodeRouteTargetIDs, right.DecodeRouteTargetIDs)
}
func compareRoutingGroups(left, right servingSnapshotGroup) int {
	return compareStrings(left.RouteTargetID, right.RouteTargetID)
}
func compareRoutingPDComponents(left, right servingSnapshotPDComponent) int {
	return compareStrings(left.RouteTargetID, right.RouteTargetID)
}
func compareRoutingPDPipelineScopes(left, right servingSnapshotPDPipelineScope) int {
	return compareStrings(left.PipelineScopeID, right.PipelineScopeID)
}

func equalRoutingEPDComponent(left, right servingSnapshotEPDComponent) bool {
	return left.RouteTargetID == right.RouteTargetID &&
		left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName &&
		left.Role == right.Role && left.Model == right.Model && left.Source == right.Source && left.Revision == right.Revision &&
		left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision &&
		equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && slices.Equal(left.Capabilities, right.Capabilities) &&
		left.Endpoint == right.Endpoint && left.PrefillBootstrapEndpoint == right.PrefillBootstrapEndpoint &&
		left.KVScopeID == right.KVScopeID && left.KVLookupScope == right.KVLookupScope &&
		left.DataParallelSize == right.DataParallelSize
}
func equalRoutingEPDPipelineScope(left, right servingSnapshotEPDPipelineScope) bool {
	return left.PipelineScopeID == right.PipelineScopeID && slices.Equal(left.EncoderRouteTargetIDs, right.EncoderRouteTargetIDs) && slices.Equal(left.PrefillRouteTargetIDs, right.PrefillRouteTargetIDs) && slices.Equal(left.DecodeRouteTargetIDs, right.DecodeRouteTargetIDs)
}
func compareRoutingEPDComponents(left, right servingSnapshotEPDComponent) int {
	return compareStrings(left.RouteTargetID, right.RouteTargetID)
}
func compareRoutingEPDPipelineScopes(left, right servingSnapshotEPDPipelineScope) int {
	return compareStrings(left.PipelineScopeID, right.PipelineScopeID)
}

func compareStrings(left, right string) int {
	if left < right {
		return -1
	}
	if left > right {
		return 1
	}
	return 0
}
