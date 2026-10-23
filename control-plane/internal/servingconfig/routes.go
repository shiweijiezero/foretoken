// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects selected model cohorts into compatible routing identities and execution paths.

package servingconfig

import (
	"cmp"
	"fmt"
	"reflect"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type RoutingIdentityConflictError struct{ Reason string }

type SplitRoutingProjectionError struct{ Service, Reason string }

// ProjectServicePDComponents publishes compatible ready prefill and decode components for one service.
func ProjectServicePDComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]PDComponent, PDPipelineScope, error) {
	var prefills, decodes []*inferencev1alpha1.ModelGroup
	for _, pool := range pools {
		if ServingRevision(service, pool) == "" {
			continue
		}
		role := pool.Spec.Template.Role
		if role != inferencev1alpha1.ModelRolePrefill && role != inferencev1alpha1.ModelRoleDecode {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if RoutingGroupOwnedBy(group, pool) && group.Spec.Revision == ServingRevision(service, pool) && RoutingGroupReady(group) && group.Spec.Role == role {
				if !vllmconfig.CompletePDRuntime(group.Spec.PDRuntime) {
					return nil, PDPipelineScope{}, &SplitRoutingProjectionError{Service: service.Name, Reason: fmt.Sprintf("Ready %s ModelGroup %q has an incomplete pdRuntime", role, group.Name)}
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
		return nil, PDPipelineScope{}, &SplitRoutingProjectionError{Service: service.Name, Reason: "requires at least one Ready prefill ModelGroup and one Ready decode ModelGroup"}
	}
	if err := validatePDGroupPairs(service.Name, prefills, decodes); err != nil {
		return nil, PDPipelineScope{}, err
	}
	pipelineScopeID := "pd:" + string(service.UID)
	components := make([]PDComponent, 0, len(prefills)+len(decodes))
	pipelineScope := PDPipelineScope{PipelineScopeID: pipelineScopeID}
	for _, group := range prefills {
		components = append(components, routingPDComponent(service, group, routingPool(pools, group), pipelineScopeID))
		pipelineScope.PrefillRouteTargetIDs = append(pipelineScope.PrefillRouteTargetIDs, string(group.UID))
	}
	for _, group := range decodes {
		components = append(components, routingPDComponent(service, group, routingPool(pools, group), pipelineScopeID))
		pipelineScope.DecodeRouteTargetIDs = append(pipelineScope.DecodeRouteTargetIDs, string(group.UID))
	}
	slices.Sort(pipelineScope.PrefillRouteTargetIDs)
	slices.Sort(pipelineScope.DecodeRouteTargetIDs)
	return components, pipelineScope, nil
}

// ProjectServiceEPDComponents publishes every Ready route in one service-local compatibility scope.
func ProjectServiceEPDComponents(service *inferencev1alpha1.ModelService, pools []*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) ([]EPDComponent, []EPDPipelineScope, error) {
	type epdScope struct {
		encoders []*inferencev1alpha1.ModelGroup
		prefills []*inferencev1alpha1.ModelGroup
		decodes  []*inferencev1alpha1.ModelGroup
	}
	byRole := map[inferencev1alpha1.ModelRole][]*inferencev1alpha1.ModelGroup{
		inferencev1alpha1.ModelRoleEncoder: {}, inferencev1alpha1.ModelRolePrefill: {}, inferencev1alpha1.ModelRoleDecode: {},
	}
	for _, pool := range pools {
		if ServingRevision(service, pool) == "" {
			continue
		}
		role := pool.Spec.Template.Role
		if _, selected := byRole[role]; !selected {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if RoutingGroupOwnedBy(group, pool) && group.Spec.Revision == ServingRevision(service, pool) && RoutingGroupReady(group) && group.Spec.Role == role {
				byRole[role] = append(byRole[role], group)
			}
		}
	}
	encoders, prefills, decodes := byRole[inferencev1alpha1.ModelRoleEncoder], byRole[inferencev1alpha1.ModelRolePrefill], byRole[inferencev1alpha1.ModelRoleDecode]
	if len(encoders) == 0 || len(prefills) == 0 || len(decodes) == 0 {
		return nil, nil, &SplitRoutingProjectionError{Service: service.Name, Reason: "requires at least one Ready encoder, prefill, and decode ModelGroup"}
	}
	slices.SortFunc(encoders, func(left, right *inferencev1alpha1.ModelGroup) int {
		return cmp.Compare(string(left.UID), string(right.UID))
	})
	slices.SortFunc(prefills, func(left, right *inferencev1alpha1.ModelGroup) int {
		return cmp.Compare(string(left.UID), string(right.UID))
	})
	slices.SortFunc(decodes, func(left, right *inferencev1alpha1.ModelGroup) int {
		return cmp.Compare(string(left.UID), string(right.UID))
	})
	var scopes []epdScope
	remainingEncoders := append([]*inferencev1alpha1.ModelGroup(nil), encoders...)
	remainingPrefills := append([]*inferencev1alpha1.ModelGroup(nil), prefills...)
	remainingDecodes := append([]*inferencev1alpha1.ModelGroup(nil), decodes...)
	for len(remainingEncoders) > 0 || len(remainingPrefills) > 0 || len(remainingDecodes) > 0 {
		if len(remainingEncoders) == 0 || len(remainingPrefills) == 0 || len(remainingDecodes) == 0 {
			return nil, nil, &SplitRoutingProjectionError{Service: service.Name, Reason: "ready E/P/D groups cannot be partitioned into complete compatible scopes"}
		}
		seedEncoder := remainingEncoders[0]
		seedPrefillIndex := slices.IndexFunc(remainingPrefills, func(candidate *inferencev1alpha1.ModelGroup) bool {
			return compatibleEncoderPrefill(seedEncoder, candidate)
		})
		if seedPrefillIndex < 0 {
			return nil, nil, &SplitRoutingProjectionError{Service: service.Name, Reason: fmt.Sprintf("encoder ModelGroup %q has no compatible prefill scope", seedEncoder.Name)}
		}
		seedPrefill := remainingPrefills[seedPrefillIndex]
		seedDecodeIndex := slices.IndexFunc(remainingDecodes, func(candidate *inferencev1alpha1.ModelGroup) bool {
			return compatiblePDGroups(seedPrefill, candidate)
		})
		if seedDecodeIndex < 0 {
			return nil, nil, &SplitRoutingProjectionError{Service: service.Name, Reason: fmt.Sprintf("prefill ModelGroup %q has no compatible decode scope", seedPrefill.Name)}
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
	var components []EPDComponent
	var pipelineScopes []EPDPipelineScope
	for index, scope := range scopes {
		if err := validatePDGroupPairs(service.Name, scope.prefills, scope.decodes); err != nil {
			return nil, nil, err
		}
		pipelineScopeID := fmt.Sprintf("epd:%s:%d", service.UID, index)
		pipelineScope := EPDPipelineScope{PipelineScopeID: pipelineScopeID}
		for _, group := range scope.encoders {
			components = append(components, routingEPDComponent(service, group, routingPool(pools, group)))
			pipelineScope.EncoderRouteTargetIDs = append(pipelineScope.EncoderRouteTargetIDs, string(group.UID))
		}
		for _, group := range scope.prefills {
			components = append(components, routingEPDComponent(service, group, routingPool(pools, group)))
			pipelineScope.PrefillRouteTargetIDs = append(pipelineScope.PrefillRouteTargetIDs, string(group.UID))
		}
		for _, group := range scope.decodes {
			components = append(components, routingEPDComponent(service, group, routingPool(pools, group)))
			pipelineScope.DecodeRouteTargetIDs = append(pipelineScope.DecodeRouteTargetIDs, string(group.UID))
		}
		slices.Sort(pipelineScope.EncoderRouteTargetIDs)
		slices.Sort(pipelineScope.PrefillRouteTargetIDs)
		slices.Sort(pipelineScope.DecodeRouteTargetIDs)
		pipelineScopes = append(pipelineScopes, pipelineScope)
	}
	if len(pipelineScopes) == 0 {
		return nil, nil, &SplitRoutingProjectionError{Service: service.Name, Reason: "no compatible Ready encoder, prefill, and decode scope"}
	}
	return components, pipelineScopes, nil
}

// RoutingGroupForService projects the Group endpoint, model identity, and Pool limits for a service route.
func RoutingGroupForService(service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool, group *inferencev1alpha1.ModelGroup) Group {
	route := routingGroup(group)
	route.MaxInputTokens = copyOptionalInt32(pool.Spec.MaxInputTokens)
	if group.Spec.Runtime.Backend == "vllm-omni" {
		route.Capabilities = []string{"video"}
	}
	route.ServiceUID, route.PoolUID, route.PoolName = string(service.UID), group.Spec.ModelPoolRef.UID, pool.Spec.PoolName
	return route
}

// ValidateRoutingIdentities rejects topology overlap and conflicting public-model identities before snapshot publication.
func ValidateRoutingIdentities(models []Model, groups []Group, components []PDComponent, epdComponents []EPDComponent) error {
	// One public model must have unambiguous stage semantics and one model/tokenizer identity.
	// Connector compatibility remains local to each P/D or E/P/D scope.
	type identity struct {
		topology, routeTargetID, revision, tokenizer, tokenizerRevision string
		source                                                          inferencev1alpha1.ModelSource
		prefixCacheIsolation                                            *bool
	}
	byModel := make(map[string]identity)
	add := func(model string, current identity) error {
		previous, exists := byModel[model]
		if !exists {
			byModel[model] = current
			return nil
		}
		if previous.topology != "" && current.topology != "" && previous.topology != current.topology {
			return &RoutingIdentityConflictError{Reason: fmt.Sprintf("public model %q is provided by both %s and %s routes", model, previous.topology, current.topology)}
		}
		if previous.source != current.source || previous.revision != current.revision || previous.tokenizer != current.tokenizer || previous.tokenizerRevision != current.tokenizerRevision || !reflect.DeepEqual(previous.prefixCacheIsolation, current.prefixCacheIsolation) {
			return &RoutingIdentityConflictError{Reason: fmt.Sprintf("public model %q has conflicting %s route identities %q and %q", model, current.topology, previous.routeTargetID, current.routeTargetID)}
		}
		if previous.topology == "" && current.topology != "" {
			byModel[model] = current
		}
		return nil
	}
	for _, model := range models {
		if err := add(model.Model, identity{topology: model.Topology, routeTargetID: model.ServiceUID, source: model.Source, revision: model.Revision, tokenizer: model.Tokenizer, tokenizerRevision: model.TokenizerRevision, prefixCacheIsolation: model.PrefixCacheIsolation}); err != nil {
			return err
		}
	}
	for _, group := range groups {
		if err := add(group.Model, identity{topology: "aggregate", routeTargetID: group.RouteTargetID, source: group.Source, revision: group.Revision, tokenizer: group.Tokenizer, tokenizerRevision: group.TokenizerRevision, prefixCacheIsolation: group.PrefixCacheIsolation}); err != nil {
			return err
		}
	}
	for _, component := range components {
		if err := add(component.Model, identity{topology: "P/D", routeTargetID: component.RouteTargetID, source: component.Source, revision: component.Revision, tokenizer: component.Tokenizer, tokenizerRevision: component.TokenizerRevision, prefixCacheIsolation: component.PrefixCacheIsolation}); err != nil {
			return err
		}
	}
	for _, component := range epdComponents {
		if err := add(component.Model, identity{topology: "E/P/D", routeTargetID: component.RouteTargetID, source: component.Source, revision: component.Revision, tokenizer: component.Tokenizer, tokenizerRevision: component.TokenizerRevision, prefixCacheIsolation: component.PrefixCacheIsolation}); err != nil {
			return err
		}
	}
	return nil
}

// ServingRevision returns the Pool cohort selected by its owning model service for routing and observation.
func ServingRevision(service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool) string {
	if service == nil || pool == nil {
		return ""
	}
	for _, selected := range service.Status.ServingPoolRevisions {
		if selected.PoolName == pool.Spec.PoolName && selected.PoolUID == string(pool.UID) {
			return selected.Revision
		}
	}
	return ""
}

// SortedServingPoolRevisions makes cohort provenance independent of status list order.
func SortedServingPoolRevisions(revisions []inferencev1alpha1.ServingPoolRevision) []inferencev1alpha1.ServingPoolRevision {
	selected := slices.Clone(revisions)
	slices.SortFunc(selected, func(left, right inferencev1alpha1.ServingPoolRevision) int {
		if compared := cmp.Compare(left.PoolName, right.PoolName); compared != 0 {
			return compared
		}
		if compared := cmp.Compare(left.PoolUID, right.PoolUID); compared != 0 {
			return compared
		}
		return cmp.Compare(left.Revision, right.Revision)
	})
	return selected
}

// OwnedRoutingPools selects live Pools whose references and controller ownership match the model service.
func OwnedRoutingPools(service *inferencev1alpha1.ModelService, pools []inferencev1alpha1.ModelPool) []*inferencev1alpha1.ModelPool {
	owned := make([]*inferencev1alpha1.ModelPool, 0)
	for index := range pools {
		if RoutingPoolOwnedBy(&pools[index], service) {
			owned = append(owned, &pools[index])
		}
	}
	return owned
}

// ServiceDeclaresEPD identifies service intent that requires an encoder stage.
func ServiceDeclaresEPD(service *inferencev1alpha1.ModelService) bool {
	return slices.ContainsFunc(service.Spec.ModelPools, func(pool inferencev1alpha1.ModelPoolTemplate) bool {
		return pool.Role == inferencev1alpha1.ModelRoleEncoder
	})
}

// ServiceDeclaresPD identifies service intent with prefill or decode stages.
func ServiceDeclaresPD(service *inferencev1alpha1.ModelService) bool {
	return slices.ContainsFunc(service.Spec.ModelPools, func(pool inferencev1alpha1.ModelPoolTemplate) bool {
		return pool.Role == inferencev1alpha1.ModelRolePrefill || pool.Role == inferencev1alpha1.ModelRoleDecode
	})
}

// PoolsHaveEPD identifies selected Pools that require encoder-prefill-decode routing.
func PoolsHaveEPD(pools []*inferencev1alpha1.ModelPool) bool {
	return slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
		return pool.Spec.Template.Role == inferencev1alpha1.ModelRoleEncoder
	})
}

// PoolsContainCompleteEPD checks that selected Pools contain every required encoder, prefill, and decode role.
func PoolsContainCompleteEPD(pools []*inferencev1alpha1.ModelPool) bool {
	for _, role := range []inferencev1alpha1.ModelRole{inferencev1alpha1.ModelRoleEncoder, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode} {
		if !slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
			return pool.Spec.Template.Role == role
		}) {
			return false
		}
	}
	return true
}

// PoolsHavePD identifies selected Pools that require prefill-decode routing.
func PoolsHavePD(pools []*inferencev1alpha1.ModelPool) bool {
	return slices.ContainsFunc(pools, func(pool *inferencev1alpha1.ModelPool) bool {
		return pool.Spec.Template.Role == inferencev1alpha1.ModelRolePrefill || pool.Spec.Template.Role == inferencev1alpha1.ModelRoleDecode
	})
}

// HasServingCohort reports whether a live model service has selected an execution cohort; Group readiness is checked separately.
func HasServingCohort(service *inferencev1alpha1.ModelService) bool {
	return service != nil && service.DeletionTimestamp.IsZero() && len(service.Status.ServingPoolRevisions) > 0
}

// RoutingPoolOwnedBy checks Pool ownership and excludes Pools already entering deletion.
func RoutingPoolOwnedBy(pool *inferencev1alpha1.ModelPool, service *inferencev1alpha1.ModelService) bool {
	return ModelPoolOwnedBy(pool, service) && pool.DeletionTimestamp.IsZero()
}

// ModelPoolOwnedBy verifies identity ownership independently of routing eligibility.
func ModelPoolOwnedBy(pool *inferencev1alpha1.ModelPool, service *inferencev1alpha1.ModelService) bool {
	return pool != nil && service != nil &&
		pool.Spec.ModelServiceRef.Name == service.Name && pool.Spec.ModelServiceRef.UID == string(service.UID) &&
		workload.ControllerOwnerMatches(pool, inferencev1alpha1.GroupVersion.String(), "ModelService", service.Name, service.UID)
}

// RoutingGroupOwnedBy checks Group ownership and excludes Groups already entering deletion.
func RoutingGroupOwnedBy(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) bool {
	return ModelGroupOwnedBy(group, pool) && group.DeletionTimestamp.IsZero()
}

// ModelGroupOwnedBy verifies immutable artifact ownership even during deletion drain.
func ModelGroupOwnedBy(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) bool {
	return group != nil && pool != nil &&
		group.Spec.ModelPoolRef.Name == pool.Name && group.Spec.ModelPoolRef.UID == string(pool.UID) &&
		workload.ControllerOwnerMatches(group, inferencev1alpha1.GroupVersion.String(), "ModelPool", pool.Name, pool.UID)
}

// RoutingGroupReady reports whether all required members of the current Group configuration are ready for routing.
func RoutingGroupReady(group *inferencev1alpha1.ModelGroup) bool {
	if !group.DeletionTimestamp.IsZero() {
		return false
	}
	ready := meta.FindStatusCondition(group.Status.Conditions, workload.ConditionReady)
	return ready != nil && ready.Status == metav1.ConditionTrue && ready.ObservedGeneration == group.Generation && group.Status.ReadyMembers == group.Spec.MemberCount
}

// RoutingCapabilities returns the declared model capabilities published to frontend consumers.
func RoutingCapabilities(features inferencev1alpha1.ModelFeatures) []string {
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

// MatchingRoutingIdentity compares the model, tokenizer, and cache isolation identity shared by compatible routes.
func MatchingRoutingIdentity(left, right Group) bool {
	return left.Model == right.Model && left.Source == right.Source && left.Revision == right.Revision &&
		left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision &&
		reflect.DeepEqual(left.PrefixCacheIsolation, right.PrefixCacheIsolation)
}

// Error describes a snapshot-wide public-model identity conflict.
func (err *RoutingIdentityConflictError) Error() string {
	return "routing projection: " + err.Reason
}

// Error describes a service-local P/D or E/P/D routing projection failure.
func (err *SplitRoutingProjectionError) Error() string {
	if err.Service == "" {
		return "split routing projection: " + err.Reason
	}
	return fmt.Sprintf("split routing projection for ModelService %q: %s", err.Service, err.Reason)
}

// routingPool returns the owner of a Group selected from these Pools.
func routingPool(pools []*inferencev1alpha1.ModelPool, group *inferencev1alpha1.ModelGroup) *inferencev1alpha1.ModelPool {
	for _, pool := range pools {
		if string(pool.UID) == group.Spec.ModelPoolRef.UID {
			return pool
		}
	}
	panic("selected routing Group has no owning Pool")
}

func compatibleEncoderPrefill(encoder, prefill *inferencev1alpha1.ModelGroup) bool {
	return MatchingRoutingIdentity(routingGroup(encoder), routingGroup(prefill)) &&
		vllmconfig.CompletePDRuntime(prefill.Spec.PDRuntime) && prefill.Spec.PDRuntime.Connector == inferencev1alpha1.MooncakeConnector &&
		vllmconfig.CompleteECRuntime(encoder.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleProducer) && vllmconfig.CompleteECRuntime(prefill.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleConsumer) &&
		matchingECRuntime(encoder.Spec.ECRuntime, prefill.Spec.ECRuntime)
}

func matchingECRuntime(left, right *inferencev1alpha1.ModelGroupECRuntimeConfig) bool {
	return left.ServiceUID == right.ServiceUID && left.Generation == right.Generation &&
		left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision &&
		left.Connector == right.Connector && left.SharedStorageClaim == right.SharedStorageClaim && left.SharedStoragePath == right.SharedStoragePath
}

// validatePDGroupPairs checks every pairing that a published P/D or E/P/D scope permits.
// Native transfer compatibility, including asymmetric TP, need not be transitive.
func validatePDGroupPairs(service string, prefills, decodes []*inferencev1alpha1.ModelGroup) error {
	for _, prefill := range prefills {
		for _, decode := range decodes {
			if !compatiblePDGroups(prefill, decode) {
				return &SplitRoutingProjectionError{Service: service, Reason: fmt.Sprintf("Ready prefill ModelGroup %q is incompatible with decode ModelGroup %q", prefill.Name, decode.Name)}
			}
		}
	}
	return nil
}

func compatiblePDGroups(prefill, decode *inferencev1alpha1.ModelGroup) bool {
	return MatchingRoutingIdentity(routingGroup(prefill), routingGroup(decode)) && vllmconfig.CompatibleKVTransfer(prefill.Spec, decode.Spec) && matchingPDRuntime(prefill.Spec.PDRuntime, decode.Spec.PDRuntime)
}

func matchingPDRuntime(left, right *inferencev1alpha1.ModelGroupPDRuntimeConfig) bool {
	return vllmconfig.CompletePDRuntime(left) && vllmconfig.CompletePDRuntime(right) &&
		left.ServiceUID == right.ServiceUID && left.Connector == right.Connector && left.Protocol == right.Protocol
}

func routingParallelism(group *inferencev1alpha1.ModelGroup) Parallelism {
	return Parallelism{
		TP:  group.Spec.Parallelism.TP,
		PP:  group.Spec.Parallelism.PP,
		DP:  group.Spec.Parallelism.DP,
		PCP: group.Spec.Parallelism.PCP,
		DCP: group.Spec.Parallelism.DCP,
		EP:  group.Spec.Parallelism.EP != nil,
	}
}

func routingGroup(group *inferencev1alpha1.ModelGroup) Group {
	return Group{
		RouteTargetID:        string(group.UID),
		Model:                group.Spec.Artifacts.Model,
		Source:               group.Spec.Artifacts.Source,
		Revision:             group.Spec.Artifacts.ModelRevision,
		Tokenizer:            group.Spec.Artifacts.Tokenizer,
		TokenizerRevision:    group.Spec.Artifacts.TokenizerRevision,
		PrefixCacheIsolation: group.Spec.Runtime.PrefixCacheIsolation,
		Capabilities:         RoutingCapabilities(group.Spec.Features),
		Endpoint:             workload.ModelGroupEndpoint(group, group.Spec.Runtime.Port),
		KVScopeID:            KVScopeID(group),
		KVLookupScope:        sharedKVLookupScope(group),
		DataParallelSize:     group.Spec.Parallelism.DP,
	}
}

func routingPDComponent(service *inferencev1alpha1.ModelService, group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool, pipelineScopeID string) PDComponent {
	pd := group.Spec.PDRuntime
	features := group.Spec.Features
	// Standalone P/D routes advertise text input; encoder scopes own multimodal input.
	features.Multimodal = nil
	component := PDComponent{
		RouteTargetID:        string(group.UID),
		ServiceUID:           string(service.UID),
		PoolUID:              group.Spec.ModelPoolRef.UID,
		PoolName:             pool.Spec.PoolName,
		Role:                 string(group.Spec.Role),
		PipelineScopeID:      pipelineScopeID,
		Model:                group.Spec.Artifacts.Model,
		Source:               group.Spec.Artifacts.Source,
		Revision:             group.Spec.Artifacts.ModelRevision,
		Tokenizer:            group.Spec.Artifacts.Tokenizer,
		TokenizerRevision:    group.Spec.Artifacts.TokenizerRevision,
		PrefixCacheIsolation: group.Spec.Runtime.PrefixCacheIsolation,
		MaxInputTokens:       copyOptionalInt32(pool.Spec.MaxInputTokens),
		Connector:            pd.Connector,
		Protocol:             pd.Protocol,
		Capabilities:         RoutingCapabilities(features),
		Endpoint:             workload.ModelGroupEndpoint(group, group.Spec.Runtime.Port),
		KVScopeID:            KVScopeID(group),
		KVLookupScope:        sharedKVLookupScope(group),
		DataParallelSize:     group.Spec.Parallelism.DP,
		Parallelism:          routingParallelism(group),
	}
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill && pd.Connector == inferencev1alpha1.MooncakeConnector {
		component.PrefillBootstrapEndpoint = workload.ModelGroupEndpoint(group, pd.BootstrapPort)
	}
	return component
}

func routingEPDComponent(service *inferencev1alpha1.ModelService, group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) EPDComponent {
	features := group.Spec.Features
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill || group.Spec.Role == inferencev1alpha1.ModelRoleDecode {
		features.Multimodal = nil
	}
	component := EPDComponent{
		RouteTargetID:        string(group.UID),
		ServiceUID:           string(service.UID),
		PoolUID:              group.Spec.ModelPoolRef.UID,
		PoolName:             pool.Spec.PoolName,
		Role:                 string(group.Spec.Role),
		Model:                group.Spec.Artifacts.Model,
		Source:               group.Spec.Artifacts.Source,
		Revision:             group.Spec.Artifacts.ModelRevision,
		Tokenizer:            group.Spec.Artifacts.Tokenizer,
		TokenizerRevision:    group.Spec.Artifacts.TokenizerRevision,
		PrefixCacheIsolation: group.Spec.Runtime.PrefixCacheIsolation,
		MaxInputTokens:       copyOptionalInt32(pool.Spec.MaxInputTokens),
		Capabilities:         RoutingCapabilities(features),
		Endpoint:             workload.ModelGroupEndpoint(group, group.Spec.Runtime.Port),
		KVScopeID:            KVScopeID(group),
		KVLookupScope:        sharedKVLookupScope(group),
		DataParallelSize:     group.Spec.Parallelism.DP,
		Parallelism:          routingParallelism(group),
	}
	if group.Spec.Role == inferencev1alpha1.ModelRolePrefill {
		component.PrefillBootstrapEndpoint = workload.ModelGroupEndpoint(group, group.Spec.PDRuntime.BootstrapPort)
	}
	return component
}

func copyOptionalInt32(value *int32) *int32 {
	if value == nil {
		return nil
	}
	copied := *value
	return &copied
}
