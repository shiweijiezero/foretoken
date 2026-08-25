// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Publishes private, versioned ModelGroup discovery snapshots to frontend Pods.

package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func (reconciler *FrontendServiceReconciler) reconcileServingSnapshot(ctx context.Context, frontend *inferencev1alpha1.FrontendService) (bool, error) {
	models, err := reconciler.projectScalingModels(ctx, frontend.Namespace)
	if err != nil {
		return false, err
	}
	groups, pdComponents, pdPipelineScopes, epdComponents, epdPipelineScopes, projectionErr := reconciler.projectableRouting(ctx, frontend.Namespace)
	if projectionErr != nil {
		var knownIncompatibility *incompatibleRoutingGroupsError
		var pdProjectionError *pdRoutingProjectionError
		switch {
		case errors.As(projectionErr, &knownIncompatibility):
			// A global identity conflict invalidates every route for that public model.
			groups, pdComponents, pdPipelineScopes, epdComponents, epdPipelineScopes = nil, nil, nil, nil, nil
		case errors.As(projectionErr, &pdProjectionError):
			// Service-local P/D or E/P/D failures have already been excluded from the partial projection.
		default:
			return false, projectionErr
		}
	}
	name := frontendServingConfigMapName(frontend)
	current := new(corev1.ConfigMap)
	err = reconciler.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: name}, current)
	if err == nil && !metav1.IsControlledBy(current, frontend) {
		return false, fmt.Errorf("ConfigMap %q is not controlled by FrontendService", name)
	}
	if err != nil && !apierrors.IsNotFound(err) {
		return false, fmt.Errorf("get serving snapshot ConfigMap: %w", err)
	}

	// Status is the durable version floor, while the persisted ConfigMap is the last semantic payload.
	// Increment only for changed content so recreation or reconcile replay cannot publish an older generation.
	version := frontend.Status.ServingSnapshotVersion
	contentsChanged := true
	var previous servingSnapshot
	if err == nil {
		if decodeErr := json.Unmarshal([]byte(current.Data[servingSnapshotKey]), &previous); decodeErr == nil {
			if previous.Version > version {
				version = previous.Version
			}
			contentsChanged = !slices.EqualFunc(previous.Models, models, equalScalingModel) || !slices.EqualFunc(previous.Groups, groups, equalRoutingGroup) || !slices.EqualFunc(previous.PDComponents, pdComponents, equalRoutingPDComponent) || !slices.EqualFunc(previous.PDPipelineScopes, pdPipelineScopes, equalRoutingPDPipelineScope) || !slices.EqualFunc(previous.EPDComponents, epdComponents, equalRoutingEPDComponent) || !slices.EqualFunc(previous.EPDPipelineScopes, epdPipelineScopes, equalRoutingEPDPipelineScope)
		}
	}
	if contentsChanged || version == 0 {
		version++
	}
	payload, err := json.Marshal(servingSnapshot{Version: version, Models: models, Groups: groups, PDComponents: pdComponents, PDPipelineScopes: pdPipelineScopes, EPDComponents: epdComponents, EPDPipelineScopes: epdPipelineScopes})
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

func (reconciler *FrontendServiceReconciler) projectScalingModels(ctx context.Context, namespace string) ([]servingSnapshotModel, error) {
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(namespace)); err != nil {
		return nil, fmt.Errorf("list ModelServices for scaling catalog: %w", err)
	}
	var pools inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(namespace)); err != nil {
		return nil, fmt.Errorf("list ModelPools for scaling catalog: %w", err)
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(namespace)); err != nil {
		return nil, fmt.Errorf("list ModelGroups for scaling catalog: %w", err)
	}

	models := make([]servingSnapshotModel, 0, len(services.Items))
	for index := range services.Items {
		service := &services.Items[index]
		if !modelServiceConfigured(service) {
			continue
		}
		servicePools := ownedRoutingPools(service, pools.Items)
		if len(service.Status.ServingPoolRevisions) > 0 {
			servicePools = slices.DeleteFunc(servicePools, func(pool *inferencev1alpha1.ModelPool) bool {
				return serviceServingRevision(service, pool) == ""
			})
		}
		admissionTargetSets := admissionTargetSetsForService(service, servicePools)
		if len(admissionTargetSets) == 0 {
			continue
		}
		if len(servicePools) == 0 {
			// E/P/D intent can provide a pipeline target before its ModelPools are
			// visible in the cache. Without a pool, there is no model identity to
			// publish, so leave this service out of the scaling catalog.
			continue
		}
		template := servicePools[0].Spec.Template
		model, revision, tokenizer, tokenizerRevision := template.Model, template.ModelRevision, template.Tokenizer, template.TokenizerRevision
		maxInputTokens, features := copyOptionalInt32(template.MaxInputTokens), template.Features
		if len(service.Status.ServingPoolRevisions) > 0 {
			var selected *inferencev1alpha1.ModelGroup
			for _, pool := range servicePools {
				revision := serviceServingRevision(service, pool)
				for groupIndex := range groups.Items {
					group := &groups.Items[groupIndex]
					if routingGroupOwnedBy(group, pool) && group.Spec.Revision == revision && routingGroupReady(group) {
						selected = group
						break
					}
				}
				if selected != nil {
					break
				}
			}
			if selected == nil {
				continue
			}
			model, revision = selected.Spec.Artifacts.Model, selected.Spec.Artifacts.ModelRevision
			tokenizer, tokenizerRevision = selected.Spec.Artifacts.Tokenizer, selected.Spec.Artifacts.TokenizerRevision
			maxInputTokens, features = copyOptionalInt32(selected.Spec.MaxInputTokens), selected.Spec.Features
			if selected.Spec.Role == inferencev1alpha1.ModelRolePrefill || selected.Spec.Role == inferencev1alpha1.ModelRoleDecode {
				features.Multimodal = nil
			}
		}
		models = append(models, servingSnapshotModel{
			ServiceUID:          string(service.UID),
			Model:               model,
			Revision:            revision,
			Tokenizer:           tokenizer,
			TokenizerRevision:   tokenizerRevision,
			MaxInputTokens:      maxInputTokens,
			Capabilities:        routingCapabilities(features),
			AdmissionTargetSets: admissionTargetSets,
		})
	}
	slices.SortFunc(models, func(left, right servingSnapshotModel) int {
		if compared := compareStrings(left.Model, right.Model); compared != 0 {
			return compared
		}
		return compareStrings(left.ServiceUID, right.ServiceUID)
	})
	return models, nil
}

func admissionTargetSetsForService(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool) [][]servingSnapshotScalingTarget {
	if poolsHaveEPD(pools) || len(pools) == 0 && serviceDeclaresEPD(service) {
		return [][]servingSnapshotScalingTarget{{{ServiceUID: string(service.UID), Name: "epd", UID: string(service.UID), Kind: string(core.TargetEPDPipelineScope)}}}
	}
	targets := make([]servingSnapshotScalingTarget, 0, len(pools))
	for _, pool := range pools {
		role := pool.Spec.Template.Role
		if role != inferencev1alpha1.ModelRoleAggregate && role != inferencev1alpha1.ModelRolePrefill && role != inferencev1alpha1.ModelRoleDecode {
			continue
		}
		targets = append(targets, servingSnapshotScalingTarget{ServiceUID: string(service.UID), Name: pool.Spec.PoolName, UID: string(pool.UID), Kind: string(core.TargetPool)})
	}
	slices.SortFunc(targets, func(left, right servingSnapshotScalingTarget) int {
		return compareStrings(left.UID, right.UID)
	})
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

func modelServiceConfigured(service *inferencev1alpha1.ModelService) bool {
	if service == nil || !service.DeletionTimestamp.IsZero() {
		return false
	}
	if len(service.Status.ServingPoolRevisions) > 0 {
		return true
	}
	if service.Status.ObservedGeneration != service.Generation {
		return false
	}
	intent := meta.FindStatusCondition(service.Status.Conditions, conditionIntentCompiled)
	pools := meta.FindStatusCondition(service.Status.Conditions, conditionPoolsMaterialized)
	return intent != nil && intent.Status == metav1.ConditionTrue && intent.ObservedGeneration == service.Generation && pools != nil && pools.Status == metav1.ConditionTrue && pools.ObservedGeneration == service.Generation
}

// projectableRouting follows only the Ready Service -> owned/Ready Pool -> owned/Ready
// Group chain. A Service declaring a P/D Pool never contributes aggregate routes.
func (reconciler *FrontendServiceReconciler) projectableRouting(ctx context.Context, namespace string) ([]servingSnapshotGroup, []servingSnapshotPDComponent, []servingSnapshotPDPipelineScope, []servingSnapshotEPDComponent, []servingSnapshotEPDPipelineScope, error) {
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(namespace)); err != nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("list ModelServices for routing: %w", err)
	}
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
	for serviceIndex := range services.Items {
		service := &services.Items[serviceIndex]
		if !modelServiceReady(service) {
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
	if err := validateRoutingIdentities(groups, pdComponents, epdComponents); err != nil {
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

func poolsHavePD(pools []*inferencev1alpha1.ModelPool) bool {
	return slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
		return pool.Spec.Template.Role == inferencev1alpha1.ModelRolePrefill || pool.Spec.Template.Role == inferencev1alpha1.ModelRoleDecode
	})
}

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
					return nil, servingSnapshotPDPipelineScope{}, &pdRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("Ready %s ModelGroup %q has an incomplete pdRuntime", role, group.Name)}
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
		return nil, servingSnapshotPDPipelineScope{}, &pdRoutingProjectionError{service: service.Name, reason: "requires at least one Ready prefill ModelGroup and one Ready decode ModelGroup"}
	}
	for _, group := range append(prefills, decodes...) {
		if !compatiblePDGroups(prefills[0], group) {
			return nil, servingSnapshotPDPipelineScope{}, &pdRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("Ready P/D ModelGroup %q conflicts with the Service P/D identity", group.Name)}
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

func projectServiceEPDComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]servingSnapshotEPDComponent, []servingSnapshotEPDPipelineScope, error) {
	// Publish only ordinal-aligned 1E:1P:1D triplets. An incomplete ordinal is withheld
	// without removing another complete triplet from the same Service.
	byRoleOrdinal := map[inferencev1alpha1.ModelRole]map[int32]*inferencev1alpha1.ModelGroup{
		inferencev1alpha1.ModelRoleEncoder: {}, inferencev1alpha1.ModelRolePrefill: {}, inferencev1alpha1.ModelRoleDecode: {},
	}
	for _, pool := range pools {
		if serviceServingRevision(service, pool) == "" {
			continue
		}
		role := pool.Spec.Template.Role
		if _, selected := byRoleOrdinal[role]; !selected {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if !routingGroupOwnedBy(group, pool) || group.Spec.Revision != serviceServingRevision(service, pool) || !routingGroupReady(group) || group.Spec.Role != role {
				continue
			}
			if byRoleOrdinal[role][group.Spec.Ordinal] != nil {
				return nil, nil, &pdRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("duplicate Ready %s ModelGroup ordinal %d", role, group.Spec.Ordinal)}
			}
			byRoleOrdinal[role][group.Spec.Ordinal] = group
		}
	}
	encoders, prefills, decodes := byRoleOrdinal[inferencev1alpha1.ModelRoleEncoder], byRoleOrdinal[inferencev1alpha1.ModelRolePrefill], byRoleOrdinal[inferencev1alpha1.ModelRoleDecode]
	ordinals := make([]int32, 0, len(encoders))
	for ordinal := range encoders {
		ordinals = append(ordinals, ordinal)
	}
	slices.Sort(ordinals)
	components := make([]servingSnapshotEPDComponent, 0, len(ordinals)*3)
	pipelineScopes := make([]servingSnapshotEPDPipelineScope, 0, len(ordinals))
	for _, ordinal := range ordinals {
		encoder, prefill, decode := encoders[ordinal], prefills[ordinal], decodes[ordinal]
		if prefill == nil || decode == nil || !compatibleEPDGroups(encoder, prefill, decode) {
			continue
		}
		pipelineScopeID := fmt.Sprintf("epd:%s:%d", service.UID, ordinal)
		components = append(components, routingEPDComponent(service, encoder, routingPoolName(pools, encoder), pipelineScopeID), routingEPDComponent(service, prefill, routingPoolName(pools, prefill), pipelineScopeID), routingEPDComponent(service, decode, routingPoolName(pools, decode), pipelineScopeID))
		pipelineScopes = append(pipelineScopes, servingSnapshotEPDPipelineScope{PipelineScopeID: pipelineScopeID, EncoderRouteTargetID: string(encoder.UID), PrefillRouteTargetID: string(prefill.UID), DecodeRouteTargetID: string(decode.UID)})
	}
	if len(pipelineScopes) == 0 {
		return nil, nil, &pdRoutingProjectionError{service: service.Name, reason: "requires at least one compatible Ready 1E:1P:1D triplet"}
	}
	return components, pipelineScopes, nil
}

func compatibleEPDGroups(encoder, prefill, decode *inferencev1alpha1.ModelGroup) bool {
	return matchingRoutingArtifacts(routingGroup(encoder), routingGroup(prefill)) && matchingRoutingArtifacts(routingGroup(prefill), routingGroup(decode)) &&
		completeECRuntime(encoder.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleProducer) && completeECRuntime(prefill.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleConsumer) &&
		matchingECRuntime(encoder.Spec.ECRuntime, prefill.Spec.ECRuntime) && compatiblePDGroups(prefill, decode)
}

func matchingECRuntime(left, right *inferencev1alpha1.ModelGroupECRuntimeConfig) bool {
	return left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.SharedStorageClaim == right.SharedStorageClaim && left.SharedStoragePath == right.SharedStoragePath
}

func routingEPDComponent(service *inferencev1alpha1.ModelService, group *inferencev1alpha1.ModelGroup, poolName, pipelineScopeID string) servingSnapshotEPDComponent {
	features := group.Spec.Features
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill || group.Spec.Role == inferencev1alpha1.ModelRoleDecode {
		features.Multimodal = nil
	}
	component := servingSnapshotEPDComponent{RouteTargetID: string(group.UID), ServiceUID: string(service.UID), PoolUID: group.Spec.ModelPoolRef.UID, PoolName: poolName, Role: string(group.Spec.Role), PipelineScopeID: pipelineScopeID, Model: group.Spec.Artifacts.Model, Revision: group.Spec.Artifacts.ModelRevision, Tokenizer: group.Spec.Artifacts.Tokenizer, TokenizerRevision: group.Spec.Artifacts.TokenizerRevision, MaxInputTokens: copyOptionalInt32(group.Spec.MaxInputTokens), Capabilities: routingCapabilities(features), Endpoint: modelGroupEndpoint(group, group.Spec.Runtime.Port), KVScopeID: kvScopeID(group), DataParallelSize: group.Spec.Parallelism.DP}
	if pd := group.Spec.PDRuntime; pd != nil {
		component.ProfileName, component.ProfileRevision = pd.ProfileName, pd.ProfileRevision
		component.Connector, component.Protocol = pd.Connector, pd.Protocol
	}
	if ec := group.Spec.ECRuntime; ec != nil {
		component.ECProfileName, component.ECProfileRevision = ec.ProfileName, ec.ProfileRevision
		component.ECConnector = ec.Connector
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
	return pool != nil && service != nil && pool.DeletionTimestamp.IsZero() &&
		pool.Spec.ModelServiceRef.Name == service.Name && pool.Spec.ModelServiceRef.UID == string(service.UID) &&
		routingControllerOwnerMatches(pool, inferencev1alpha1.GroupVersion.String(), "ModelService", service.Name, service.UID)
}

func routingGroupOwnedBy(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) bool {
	return group != nil && pool != nil && group.DeletionTimestamp.IsZero() &&
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

type incompatibleRoutingGroupsError struct{ first, second, model string }

func (err *incompatibleRoutingGroupsError) Error() string {
	return fmt.Sprintf("incompatible ModelGroups for public model %q: %q and %q must have identical modelRevision, tokenizer, and tokenizerRevision", err.model, err.first, err.second)
}

type pdRoutingProjectionError struct{ service, reason string }

func (err *pdRoutingProjectionError) Error() string {
	if err.service == "" {
		return "P/D routing projection: " + err.reason
	}
	return fmt.Sprintf("P/D routing projection for ModelService %q: %s", err.service, err.reason)
}

func routingGroup(group *inferencev1alpha1.ModelGroup) servingSnapshotGroup {
	return servingSnapshotGroup{RouteTargetID: string(group.UID), Model: group.Spec.Artifacts.Model, Revision: group.Spec.Artifacts.ModelRevision, Tokenizer: group.Spec.Artifacts.Tokenizer, TokenizerRevision: group.Spec.Artifacts.TokenizerRevision, MaxInputTokens: copyOptionalInt32(group.Spec.MaxInputTokens), Capabilities: routingCapabilities(group.Spec.Features), Endpoint: modelGroupEndpoint(group, group.Spec.Runtime.Port), KVScopeID: kvScopeID(group), DataParallelSize: group.Spec.Parallelism.DP}
}
func routingGroupForService(service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool, group *inferencev1alpha1.ModelGroup) servingSnapshotGroup {
	route := routingGroup(group)
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
	return matchingRoutingArtifacts(routingGroup(prefill), routingGroup(decode)) && kvScopeID(prefill) == kvScopeID(decode) && matchingPDRuntime(prefill.Spec.PDRuntime, decode.Spec.PDRuntime)
}

func completePDRuntime(runtime *inferencev1alpha1.ModelGroupPDRuntimeConfig) bool {
	return runtime != nil && runtime.ProfileName != "" && runtime.ProfileRevision != "" && runtime.Connector == "MooncakeConnector" && runtime.Protocol == "rdma" && runtime.BootstrapPort > 0 && runtime.AbortRequestTimeoutSeconds > 0 && runtime.RDMADeviceName != "" && runtime.RDMAResourceName != "" && runtime.RDMAResourceCount > 0
}

func matchingPDRuntime(left, right *inferencev1alpha1.ModelGroupPDRuntimeConfig) bool {
	return completePDRuntime(left) && completePDRuntime(right) && left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.Protocol == right.Protocol && left.BootstrapPort == right.BootstrapPort && left.AbortRequestTimeoutSeconds == right.AbortRequestTimeoutSeconds && left.RDMADeviceName == right.RDMADeviceName && left.RDMAResourceName == right.RDMAResourceName && left.RDMAResourceCount == right.RDMAResourceCount
}

func routingPDComponent(service *inferencev1alpha1.ModelService, group *inferencev1alpha1.ModelGroup, poolName, pipelineScopeID string) servingSnapshotPDComponent {
	pd := group.Spec.PDRuntime
	features := group.Spec.Features
	// No P/D runtime profile currently verifies multimodal support. Never publish
	// it from P/D routes, including objects created before API validation existed.
	features.Multimodal = nil
	component := servingSnapshotPDComponent{RouteTargetID: string(group.UID), ServiceUID: string(service.UID), PoolUID: group.Spec.ModelPoolRef.UID, PoolName: poolName, Role: string(group.Spec.Role), PipelineScopeID: pipelineScopeID, Model: group.Spec.Artifacts.Model, Revision: group.Spec.Artifacts.ModelRevision, Tokenizer: group.Spec.Artifacts.Tokenizer, TokenizerRevision: group.Spec.Artifacts.TokenizerRevision, MaxInputTokens: copyOptionalInt32(group.Spec.MaxInputTokens), ProfileName: pd.ProfileName, ProfileRevision: pd.ProfileRevision, Connector: pd.Connector, Protocol: pd.Protocol, Capabilities: routingCapabilities(features), Endpoint: modelGroupEndpoint(group, group.Spec.Runtime.Port), KVScopeID: kvScopeID(group), DataParallelSize: group.Spec.Parallelism.DP}
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
		component.PrefillBootstrapEndpoint = modelGroupEndpoint(group, pd.BootstrapPort)
	}
	return component
}

func modelGroupEndpoint(group *inferencev1alpha1.ModelGroup, port int32) string {
	return fmt.Sprintf("http://%s.%s.svc:%d", modelGroupServiceName(group), group.Namespace, port)
}

func validateRoutingIdentities(groups []servingSnapshotGroup, components []servingSnapshotPDComponent, epdComponents []servingSnapshotEPDComponent) error {
	// One public model must have unambiguous stage semantics. Reject identity conflicts and
	// overlap between aggregate, P/D, and E/P/D before the snapshot reaches any frontend.
	for first := range groups {
		for second := first + 1; second < len(groups); second++ {
			if groups[first].Model == groups[second].Model && !matchingRoutingArtifacts(groups[first], groups[second]) {
				return &incompatibleRoutingGroupsError{first: groups[first].RouteTargetID, second: groups[second].RouteTargetID, model: groups[first].Model}
			}
		}
		for _, component := range components {
			if groups[first].Model == component.Model {
				return &pdRoutingProjectionError{reason: fmt.Sprintf("public model %q is provided by both aggregate and P/D routes", component.Model)}
			}
		}
	}
	for _, component := range epdComponents {
		for _, aggregate := range groups {
			if aggregate.Model == component.Model {
				return &pdRoutingProjectionError{reason: fmt.Sprintf("public model %q is provided by both aggregate and E/P/D routes", component.Model)}
			}
		}
		for _, pd := range components {
			if pd.Model == component.Model {
				return &pdRoutingProjectionError{reason: fmt.Sprintf("public model %q is provided by both P/D and E/P/D routes", component.Model)}
			}
		}
	}
	for first := range components {
		for second := first + 1; second < len(components); second++ {
			left, right := components[first], components[second]
			if left.Model == right.Model && (left.Revision != right.Revision || left.Tokenizer != right.Tokenizer || left.TokenizerRevision != right.TokenizerRevision || left.ProfileName != right.ProfileName || left.ProfileRevision != right.ProfileRevision || left.Connector != right.Connector || left.Protocol != right.Protocol) {
				return &pdRoutingProjectionError{reason: fmt.Sprintf("public model %q has conflicting P/D route identities", left.Model)}
			}
		}
	}
	return nil
}
func matchingRoutingArtifacts(left, right servingSnapshotGroup) bool {
	return left.Model == right.Model && left.Revision == right.Revision && left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision
}
func equalScalingModel(left, right servingSnapshotModel) bool {
	return left.ServiceUID == right.ServiceUID && left.Model == right.Model && left.Revision == right.Revision && left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision && equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && slices.Equal(left.Capabilities, right.Capabilities) && slices.EqualFunc(left.AdmissionTargetSets, right.AdmissionTargetSets, func(left, right []servingSnapshotScalingTarget) bool { return slices.Equal(left, right) })
}

func equalRoutingGroup(left, right servingSnapshotGroup) bool {
	return left.RouteTargetID == right.RouteTargetID && left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName && matchingRoutingArtifacts(left, right) && equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && left.Endpoint == right.Endpoint && left.KVScopeID == right.KVScopeID && left.DataParallelSize == right.DataParallelSize && slices.Equal(left.Capabilities, right.Capabilities)
}
func equalRoutingPDComponent(left, right servingSnapshotPDComponent) bool {
	return left.RouteTargetID == right.RouteTargetID && left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName && left.Role == right.Role && left.PipelineScopeID == right.PipelineScopeID && left.Model == right.Model && left.Revision == right.Revision && left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision && equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.Protocol == right.Protocol && left.Endpoint == right.Endpoint && left.PrefillBootstrapEndpoint == right.PrefillBootstrapEndpoint && left.KVScopeID == right.KVScopeID && left.DataParallelSize == right.DataParallelSize && slices.Equal(left.Capabilities, right.Capabilities)
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
	return left.RouteTargetID == right.RouteTargetID && left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName && left.Role == right.Role && left.PipelineScopeID == right.PipelineScopeID && left.Model == right.Model && left.Revision == right.Revision && left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision && equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.Protocol == right.Protocol && left.ECProfileName == right.ECProfileName && left.ECProfileRevision == right.ECProfileRevision && left.ECConnector == right.ECConnector && slices.Equal(left.Capabilities, right.Capabilities) && left.Endpoint == right.Endpoint && left.PrefillBootstrapEndpoint == right.PrefillBootstrapEndpoint && left.KVScopeID == right.KVScopeID && left.DataParallelSize == right.DataParallelSize
}
func equalRoutingEPDPipelineScope(left, right servingSnapshotEPDPipelineScope) bool {
	return left.PipelineScopeID == right.PipelineScopeID && left.EncoderRouteTargetID == right.EncoderRouteTargetID && left.PrefillRouteTargetID == right.PrefillRouteTargetID && left.DecodeRouteTargetID == right.DecodeRouteTargetID
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
