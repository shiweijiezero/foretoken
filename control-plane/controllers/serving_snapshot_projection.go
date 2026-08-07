// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Publishes private, versioned ModelGroup discovery snapshots to frontend Pods.

package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const servingSnapshotKey = "serving.json"

type servingSnapshot struct {
	Version       uint64                        `json:"version"`
	Groups        []servingSnapshotGroup        `json:"groups"`
	PDComponents  []servingSnapshotPDComponent  `json:"pd_components,omitempty"`
	PDDomains     []servingSnapshotPDDomain     `json:"pd_domains,omitempty"`
	EPDComponents []servingSnapshotEPDComponent `json:"epd_components,omitempty"`
	EPDDomains    []servingSnapshotEPDDomain    `json:"epd_domains,omitempty"`
}

type servingSnapshotGroup struct {
	BackendID         string   `json:"backend_id"`
	Model             string   `json:"model"`
	Revision          string   `json:"revision"`
	Tokenizer         string   `json:"tokenizer"`
	TokenizerRevision string   `json:"tokenizer_revision"`
	MaxInputTokens    *int32   `json:"max_input_tokens,omitempty"`
	Capabilities      []string `json:"capabilities,omitempty"`
	Endpoint          string   `json:"endpoint"`
	KVScopeID         string   `json:"kv_scope_id"`
}

// servingSnapshotPDComponent is one P or D component. Domains express compatibility without P×D materialization.
type servingSnapshotPDComponent struct {
	BackendID                string   `json:"backend_id"`
	Role                     string   `json:"role"`
	DomainID                 string   `json:"domain_id"`
	Model                    string   `json:"model"`
	Revision                 string   `json:"revision"`
	Tokenizer                string   `json:"tokenizer"`
	TokenizerRevision        string   `json:"tokenizer_revision"`
	MaxInputTokens           *int32   `json:"max_input_tokens,omitempty"`
	ProfileName              string   `json:"profile_name"`
	ProfileRevision          string   `json:"profile_revision"`
	Connector                string   `json:"connector"`
	Protocol                 string   `json:"protocol"`
	Capabilities             []string `json:"capabilities"`
	Endpoint                 string   `json:"endpoint"`
	PrefillBootstrapEndpoint string   `json:"prefill_bootstrap_endpoint,omitempty"`
	KVScopeID                string   `json:"kv_scope_id"`
}
type servingSnapshotPDDomain struct {
	DomainID          string   `json:"domain_id"`
	PrefillBackendIDs []string `json:"prefill_backend_ids"`
	DecodeBackendIDs  []string `json:"decode_backend_ids"`
}

// servingSnapshotEPDComponent is one component of an atomic 1E:1P:1D triplet.
type servingSnapshotEPDComponent struct {
	BackendID                string   `json:"backend_id"`
	Role                     string   `json:"role"`
	DomainID                 string   `json:"domain_id"`
	Model                    string   `json:"model"`
	Revision                 string   `json:"revision"`
	Tokenizer                string   `json:"tokenizer"`
	TokenizerRevision        string   `json:"tokenizer_revision"`
	MaxInputTokens           *int32   `json:"max_input_tokens,omitempty"`
	ProfileName              string   `json:"profile_name,omitempty"`
	ProfileRevision          string   `json:"profile_revision,omitempty"`
	Connector                string   `json:"connector,omitempty"`
	Protocol                 string   `json:"protocol,omitempty"`
	ECProfileName            string   `json:"ec_profile_name,omitempty"`
	ECProfileRevision        string   `json:"ec_profile_revision,omitempty"`
	ECConnector              string   `json:"ec_connector,omitempty"`
	ECRole                   string   `json:"ec_role,omitempty"`
	ECRuntimeFingerprint     string   `json:"ec_runtime_fingerprint,omitempty"`
	Capabilities             []string `json:"capabilities"`
	Endpoint                 string   `json:"endpoint"`
	PrefillBootstrapEndpoint string   `json:"prefill_bootstrap_endpoint,omitempty"`
	KVScopeID                string   `json:"kv_scope_id"`
}

type servingSnapshotEPDDomain struct {
	DomainID         string `json:"domain_id"`
	EncoderBackendID string `json:"encoder_backend_id"`
	PrefillBackendID string `json:"prefill_backend_id"`
	DecodeBackendID  string `json:"decode_backend_id"`
}

func (reconciler *FrontendServiceReconciler) reconcileServingSnapshot(ctx context.Context, frontend *inferencev1alpha1.FrontendService) (bool, error) {
	groups, pdComponents, pdDomains, epdComponents, epdDomains, projectionErr := reconciler.projectableRouting(ctx, frontend.Namespace)
	if projectionErr != nil {
		var knownIncompatibility *incompatibleRoutingGroupsError
		var pdProjectionError *pdRoutingProjectionError
		if !errors.As(projectionErr, &knownIncompatibility) && !errors.As(projectionErr, &pdProjectionError) {
			return false, projectionErr
		}
		// A known invalid inventory must replace, rather than leave, stale routes.
		groups, pdComponents, pdDomains, epdComponents, epdDomains = nil, nil, nil, nil, nil
	}
	name := frontendServingConfigMapName(frontend)
	current := new(corev1.ConfigMap)
	err := reconciler.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: name}, current)
	if err == nil && !metav1.IsControlledBy(current, frontend) {
		return false, fmt.Errorf("ConfigMap %q is not controlled by FrontendService", name)
	}
	if err != nil && !apierrors.IsNotFound(err) {
		return false, fmt.Errorf("get serving snapshot ConfigMap: %w", err)
	}

	version := frontend.Status.ServingSnapshotVersion
	contentsChanged := true
	var previous servingSnapshot
	if err == nil {
		if decodeErr := json.Unmarshal([]byte(current.Data[servingSnapshotKey]), &previous); decodeErr == nil {
			if previous.Version > version {
				version = previous.Version
			}
			contentsChanged = !slices.EqualFunc(previous.Groups, groups, equalRoutingGroup) || !slices.EqualFunc(previous.PDComponents, pdComponents, equalRoutingPDComponent) || !slices.EqualFunc(previous.PDDomains, pdDomains, equalRoutingPDDomain) || !slices.EqualFunc(previous.EPDComponents, epdComponents, equalRoutingEPDComponent) || !slices.EqualFunc(previous.EPDDomains, epdDomains, equalRoutingEPDDomain)
		}
	}
	if contentsChanged || version == 0 {
		version++
	}
	payload, err := json.Marshal(servingSnapshot{Version: version, Groups: groups, PDComponents: pdComponents, PDDomains: pdDomains, EPDComponents: epdComponents, EPDDomains: epdDomains})
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
	return len(groups) > 0 || len(pdComponents) > 0 || len(epdComponents) > 0, nil
}

// projectableGroups retains the aggregate-only projection for callers that need it.
func (reconciler *FrontendServiceReconciler) projectableGroups(ctx context.Context, namespace string) ([]servingSnapshotGroup, error) {
	groups, _, _, _, _, err := reconciler.projectableRouting(ctx, namespace)
	return groups, err
}

// projectableRouting follows only the Ready Service -> owned/Ready Pool -> owned/Ready
// Group chain. A Service declaring a P/D Pool never contributes aggregate routes.
func (reconciler *FrontendServiceReconciler) projectableRouting(ctx context.Context, namespace string) ([]servingSnapshotGroup, []servingSnapshotPDComponent, []servingSnapshotPDDomain, []servingSnapshotEPDComponent, []servingSnapshotEPDDomain, error) {
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
	pdDomains := make([]servingSnapshotPDDomain, 0)
	epdComponents := make([]servingSnapshotEPDComponent, 0)
	epdDomains := make([]servingSnapshotEPDDomain, 0)
	var projectionErr error
	for serviceIndex := range services.Items {
		service := &services.Items[serviceIndex]
		if !modelServiceReady(service) {
			continue
		}
		servicePools := ownedRoutingPools(service, pools.Items)
		if serviceHasEPDIntent(service, servicePools) {
			components, domains, err := projectServiceEPDComponents(service, servicePools, modelGroups.Items)
			if err != nil {
				// An incomplete E/P/D Service must not withdraw other Services' routes.
				projectionErr = errors.Join(projectionErr, err)
				continue
			}
			epdComponents = append(epdComponents, components...)
			epdDomains = append(epdDomains, domains...)
			continue
		}
		if serviceHasPDIntent(service, servicePools) {
			components, domain, err := projectServicePDComponents(service, servicePools, modelGroups.Items)
			if err != nil {
				// A transiently incomplete P/D Service must not withdraw other Services' routes.
				projectionErr = errors.Join(projectionErr, err)
				continue
			}
			pdComponents = append(pdComponents, components...)
			pdDomains = append(pdDomains, domain)
			continue
		}
		for poolIndex := range servicePools {
			pool := servicePools[poolIndex]
			if !modelPoolReady(pool) {
				continue
			}
			for groupIndex := range modelGroups.Items {
				group := &modelGroups.Items[groupIndex]
				if !routingGroupOwnedBy(group, pool) || group.Spec.Revision != pool.Status.ActiveRevision || !routingGroupReady(group) || group.Spec.Role != inferencev1alpha1.ModelRoleAggregate {
					continue
				}
				groups = append(groups, routingGroup(group))
			}
		}
	}
	slices.SortFunc(groups, compareRoutingGroups)
	slices.SortFunc(pdComponents, compareRoutingPDComponents)
	slices.SortFunc(pdDomains, compareRoutingPDDomains)
	slices.SortFunc(epdComponents, compareRoutingEPDComponents)
	slices.SortFunc(epdDomains, compareRoutingEPDDomains)
	if err := validateRoutingIdentities(groups, pdComponents, epdComponents); err != nil {
		return nil, nil, nil, nil, nil, err
	}
	if projectionErr != nil && len(groups) == 0 && len(pdComponents) == 0 && len(epdComponents) == 0 {
		return nil, nil, nil, nil, nil, projectionErr
	}
	return groups, pdComponents, pdDomains, epdComponents, epdDomains, nil
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

func serviceHasEPDIntent(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool) bool {
	for _, template := range service.Spec.ModelPools {
		if template.Role == inferencev1alpha1.ModelRoleEncoder {
			return true
		}
	}
	for _, pool := range pools {
		if pool.Spec.Template.Role == inferencev1alpha1.ModelRoleEncoder {
			return true
		}
	}
	return false
}

func serviceHasPDIntent(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool) bool {
	for _, template := range service.Spec.ModelPools {
		if template.Role == inferencev1alpha1.ModelRolePrefill || template.Role == inferencev1alpha1.ModelRoleDecode {
			return true
		}
	}
	for _, pool := range pools {
		if pool.Spec.Template.Role == inferencev1alpha1.ModelRolePrefill || pool.Spec.Template.Role == inferencev1alpha1.ModelRoleDecode {
			return true
		}
	}
	return false
}

func projectServicePDComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]servingSnapshotPDComponent, servingSnapshotPDDomain, error) {
	var prefills, decodes []*inferencev1alpha1.ModelGroup
	for _, pool := range pools {
		if !modelPoolReady(pool) {
			continue
		}
		role := pool.Spec.Template.Role
		if role != inferencev1alpha1.ModelRolePrefill && role != inferencev1alpha1.ModelRoleDecode {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if routingGroupOwnedBy(group, pool) && group.Spec.Revision == pool.Status.ActiveRevision && routingGroupReady(group) && group.Spec.Role == role {
				if !completePDRuntime(group.Spec.PDRuntime) {
					return nil, servingSnapshotPDDomain{}, &pdRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("Ready %s ModelGroup %q has an incomplete pdRuntime", role, group.Name)}
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
		return nil, servingSnapshotPDDomain{}, &pdRoutingProjectionError{service: service.Name, reason: "requires at least one Ready prefill ModelGroup and one Ready decode ModelGroup"}
	}
	for _, group := range append(prefills, decodes...) {
		if !compatiblePDGroups(prefills[0], group) {
			return nil, servingSnapshotPDDomain{}, &pdRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("Ready P/D ModelGroup %q conflicts with the Service P/D identity", group.Name)}
		}
	}
	domainID := "pd:" + string(service.UID)
	components := make([]servingSnapshotPDComponent, 0, len(prefills)+len(decodes))
	domain := servingSnapshotPDDomain{DomainID: domainID}
	for _, group := range prefills {
		components = append(components, routingPDComponent(group, domainID))
		domain.PrefillBackendIDs = append(domain.PrefillBackendIDs, string(group.UID))
	}
	for _, group := range decodes {
		components = append(components, routingPDComponent(group, domainID))
		domain.DecodeBackendIDs = append(domain.DecodeBackendIDs, string(group.UID))
	}
	slices.Sort(domain.PrefillBackendIDs)
	slices.Sort(domain.DecodeBackendIDs)
	return components, domain, nil
}

func projectServiceEPDComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]servingSnapshotEPDComponent, []servingSnapshotEPDDomain, error) {
	byRoleOrdinal := map[inferencev1alpha1.ModelRole]map[int32]*inferencev1alpha1.ModelGroup{
		inferencev1alpha1.ModelRoleEncoder: {}, inferencev1alpha1.ModelRolePrefill: {}, inferencev1alpha1.ModelRoleDecode: {},
	}
	for _, pool := range pools {
		if !modelPoolReady(pool) {
			continue
		}
		role := pool.Spec.Template.Role
		if _, selected := byRoleOrdinal[role]; !selected {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if !routingGroupOwnedBy(group, pool) || group.Spec.Revision != pool.Status.ActiveRevision || !routingGroupReady(group) || group.Spec.Role != role {
				continue
			}
			if byRoleOrdinal[role][group.Spec.Ordinal] != nil {
				return nil, nil, &pdRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("duplicate Ready %s ModelGroup ordinal %d", role, group.Spec.Ordinal)}
			}
			byRoleOrdinal[role][group.Spec.Ordinal] = group
		}
	}
	encoders, prefills, decodes := byRoleOrdinal[inferencev1alpha1.ModelRoleEncoder], byRoleOrdinal[inferencev1alpha1.ModelRolePrefill], byRoleOrdinal[inferencev1alpha1.ModelRoleDecode]
	if len(encoders) == 0 || len(encoders) != len(prefills) || len(encoders) != len(decodes) {
		return nil, nil, &pdRoutingProjectionError{service: service.Name, reason: "requires complete Ready 1E:1P:1D triplets"}
	}
	components := make([]servingSnapshotEPDComponent, 0, len(encoders)*3)
	domains := make([]servingSnapshotEPDDomain, 0, len(encoders))
	for ordinal, encoder := range encoders {
		prefill, decode := prefills[ordinal], decodes[ordinal]
		if prefill == nil || decode == nil || !compatibleEPDGroups(encoder, prefill, decode) {
			return nil, nil, &pdRoutingProjectionError{service: service.Name, reason: fmt.Sprintf("Ready E/P/D ModelGroups at ordinal %d have incompatible runtime identities", ordinal)}
		}
		domainID := fmt.Sprintf("epd:%s:%d", service.UID, ordinal)
		components = append(components, routingEPDComponent(encoder, domainID), routingEPDComponent(prefill, domainID), routingEPDComponent(decode, domainID))
		domains = append(domains, servingSnapshotEPDDomain{DomainID: domainID, EncoderBackendID: string(encoder.UID), PrefillBackendID: string(prefill.UID), DecodeBackendID: string(decode.UID)})
	}
	return components, domains, nil
}

func compatibleEPDGroups(encoder, prefill, decode *inferencev1alpha1.ModelGroup) bool {
	return matchingRoutingArtifacts(routingGroup(encoder), routingGroup(prefill)) && matchingRoutingArtifacts(routingGroup(prefill), routingGroup(decode)) &&
		completeECRuntime(encoder.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleProducer) && completeECRuntime(prefill.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleConsumer) &&
		matchingECRuntime(encoder.Spec.ECRuntime, prefill.Spec.ECRuntime) && compatiblePDGroups(prefill, decode)
}

func matchingECRuntime(left, right *inferencev1alpha1.ModelGroupECRuntimeConfig) bool {
	return left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.RuntimeFingerprint == right.RuntimeFingerprint && left.SharedStorageClaim == right.SharedStorageClaim && left.SharedStoragePath == right.SharedStoragePath
}

func routingEPDComponent(group *inferencev1alpha1.ModelGroup, domainID string) servingSnapshotEPDComponent {
	features := group.Spec.Features
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill || group.Spec.Role == inferencev1alpha1.ModelRoleDecode {
		features.Multimodal = nil
	}
	component := servingSnapshotEPDComponent{BackendID: string(group.UID), Role: string(group.Spec.Role), DomainID: domainID, Model: group.Spec.Artifacts.Model, Revision: group.Spec.Artifacts.ModelRevision, Tokenizer: group.Spec.Artifacts.Tokenizer, TokenizerRevision: group.Spec.Artifacts.TokenizerRevision, MaxInputTokens: copyOptionalInt32(group.Spec.MaxInputTokens), Capabilities: routingCapabilities(features), Endpoint: modelGroupEndpoint(group, group.Spec.Runtime.Port), KVScopeID: kvScopeID(group)}
	if pd := group.Spec.PDRuntime; pd != nil {
		component.ProfileName, component.ProfileRevision = pd.ProfileName, pd.ProfileRevision
		component.Connector, component.Protocol = pd.Connector, pd.Protocol
	}
	if ec := group.Spec.ECRuntime; ec != nil {
		component.ECProfileName, component.ECProfileRevision = ec.ProfileName, ec.ProfileRevision
		component.ECConnector, component.ECRole = ec.Connector, string(ec.Role)
		component.ECRuntimeFingerprint = ec.RuntimeFingerprint
	}
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
		component.PrefillBootstrapEndpoint = modelGroupEndpoint(group, group.Spec.PDRuntime.BootstrapPort)
	}
	return component
}

func modelServiceReady(service *inferencev1alpha1.ModelService) bool {
	if service == nil || !service.DeletionTimestamp.IsZero() || service.Status.ObservedGeneration != service.Generation {
		return false
	}
	ready := meta.FindStatusCondition(service.Status.Conditions, conditionReady)
	return ready != nil && ready.Status == metav1.ConditionTrue && ready.ObservedGeneration == service.Generation
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
	return servingSnapshotGroup{BackendID: string(group.UID), Model: group.Spec.Artifacts.Model, Revision: group.Spec.Artifacts.ModelRevision, Tokenizer: group.Spec.Artifacts.Tokenizer, TokenizerRevision: group.Spec.Artifacts.TokenizerRevision, MaxInputTokens: copyOptionalInt32(group.Spec.MaxInputTokens), Capabilities: routingCapabilities(group.Spec.Features), Endpoint: modelGroupEndpoint(group, group.Spec.Runtime.Port), KVScopeID: kvScopeID(group)}
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
		switch modality {
		case inferencev1alpha1.MultimodalModalityImage:
			optional["multimodal.image"] = struct{}{}
		case inferencev1alpha1.MultimodalModalityVideo:
			optional["multimodal.video"] = struct{}{}
		case inferencev1alpha1.MultimodalModalityAudio:
			optional["multimodal.audio"] = struct{}{}
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
	return runtime != nil && runtime.ProfileName != "" && runtime.ProfileRevision != "" && runtime.Connector == "MooncakeConnector" && runtime.Protocol == "rdma" && runtime.BootstrapPort > 0 && runtime.AbortRequestTimeoutSeconds > 0
}

func matchingPDRuntime(left, right *inferencev1alpha1.ModelGroupPDRuntimeConfig) bool {
	return completePDRuntime(left) && completePDRuntime(right) && left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.Protocol == right.Protocol && left.BootstrapPort == right.BootstrapPort && left.AbortRequestTimeoutSeconds == right.AbortRequestTimeoutSeconds
}

func routingPDComponent(group *inferencev1alpha1.ModelGroup, domainID string) servingSnapshotPDComponent {
	pd := group.Spec.PDRuntime
	features := group.Spec.Features
	// No P/D runtime profile currently verifies multimodal support. Never publish
	// it from P/D routes, including objects created before API validation existed.
	features.Multimodal = nil
	component := servingSnapshotPDComponent{BackendID: string(group.UID), Role: string(group.Spec.Role), DomainID: domainID, Model: group.Spec.Artifacts.Model, Revision: group.Spec.Artifacts.ModelRevision, Tokenizer: group.Spec.Artifacts.Tokenizer, TokenizerRevision: group.Spec.Artifacts.TokenizerRevision, MaxInputTokens: copyOptionalInt32(group.Spec.MaxInputTokens), ProfileName: pd.ProfileName, ProfileRevision: pd.ProfileRevision, Connector: pd.Connector, Protocol: pd.Protocol, Capabilities: routingCapabilities(features), Endpoint: modelGroupEndpoint(group, group.Spec.Runtime.Port), KVScopeID: kvScopeID(group)}
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
		component.PrefillBootstrapEndpoint = modelGroupEndpoint(group, pd.BootstrapPort)
	}
	return component
}

func modelGroupEndpoint(group *inferencev1alpha1.ModelGroup, port int32) string {
	return fmt.Sprintf("http://%s.%s.svc:%d", group.Name, group.Namespace, port)
}

func validateRoutingIdentities(groups []servingSnapshotGroup, components []servingSnapshotPDComponent, epdComponents []servingSnapshotEPDComponent) error {
	for first := range groups {
		for second := first + 1; second < len(groups); second++ {
			if groups[first].Model == groups[second].Model && !matchingRoutingArtifacts(groups[first], groups[second]) {
				return &incompatibleRoutingGroupsError{first: groups[first].BackendID, second: groups[second].BackendID, model: groups[first].Model}
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
func equalRoutingGroup(left, right servingSnapshotGroup) bool {
	return left.BackendID == right.BackendID && matchingRoutingArtifacts(left, right) && equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && left.Endpoint == right.Endpoint && left.KVScopeID == right.KVScopeID && slices.Equal(left.Capabilities, right.Capabilities)
}
func equalRoutingPDComponent(left, right servingSnapshotPDComponent) bool {
	return left.BackendID == right.BackendID && left.Role == right.Role && left.DomainID == right.DomainID && left.Model == right.Model && left.Revision == right.Revision && left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision && equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.Protocol == right.Protocol && left.Endpoint == right.Endpoint && left.PrefillBootstrapEndpoint == right.PrefillBootstrapEndpoint && left.KVScopeID == right.KVScopeID && slices.Equal(left.Capabilities, right.Capabilities)
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
func equalRoutingPDDomain(left, right servingSnapshotPDDomain) bool {
	return left.DomainID == right.DomainID && slices.Equal(left.PrefillBackendIDs, right.PrefillBackendIDs) && slices.Equal(left.DecodeBackendIDs, right.DecodeBackendIDs)
}
func compareRoutingGroups(left, right servingSnapshotGroup) int {
	return compareStrings(left.BackendID, right.BackendID)
}
func compareRoutingPDComponents(left, right servingSnapshotPDComponent) int {
	return compareStrings(left.BackendID, right.BackendID)
}
func compareRoutingPDDomains(left, right servingSnapshotPDDomain) int {
	return compareStrings(left.DomainID, right.DomainID)
}

func equalRoutingEPDComponent(left, right servingSnapshotEPDComponent) bool {
	return left.BackendID == right.BackendID && left.Role == right.Role && left.DomainID == right.DomainID && left.Model == right.Model && left.Revision == right.Revision && left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision && equalOptionalInt32(left.MaxInputTokens, right.MaxInputTokens) && left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision && left.Connector == right.Connector && left.Protocol == right.Protocol && left.ECProfileName == right.ECProfileName && left.ECProfileRevision == right.ECProfileRevision && left.ECConnector == right.ECConnector && left.ECRole == right.ECRole && left.ECRuntimeFingerprint == right.ECRuntimeFingerprint && slices.Equal(left.Capabilities, right.Capabilities) && left.Endpoint == right.Endpoint && left.PrefillBootstrapEndpoint == right.PrefillBootstrapEndpoint && left.KVScopeID == right.KVScopeID
}
func equalRoutingEPDDomain(left, right servingSnapshotEPDDomain) bool {
	return left.DomainID == right.DomainID && left.EncoderBackendID == right.EncoderBackendID && left.PrefillBackendID == right.PrefillBackendID && left.DecodeBackendID == right.DecodeBackendID
}
func compareRoutingEPDComponents(left, right servingSnapshotEPDComponent) int {
	return compareStrings(left.BackendID, right.BackendID)
}
func compareRoutingEPDDomains(left, right servingSnapshotEPDDomain) int {
	return compareStrings(left.DomainID, right.DomainID)
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
