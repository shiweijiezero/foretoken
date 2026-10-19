// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compares the configuration fields that determine a serving publication version.
package servingconfig

import (
	"reflect"
	"slices"

	"k8s.io/utils/ptr"
)

// SameConfiguration tells the frontend publisher whether settings, membership, or routes changed.
// Publication versions are compared separately by the lifecycle owner.
func (snapshot Snapshot) SameConfiguration(other Snapshot) bool {
	return reflect.DeepEqual(snapshot.Settings, other.Settings) &&
		slices.Equal(snapshot.FrontendInstances, other.FrontendInstances) &&
		slices.Equal(snapshot.BackendInstances, other.BackendInstances) &&
		reflect.DeepEqual(snapshot.Admission, other.Admission) &&
		slices.EqualFunc(snapshot.Models, other.Models, equalScalingModel) &&
		slices.EqualFunc(snapshot.Groups, other.Groups, equalRoutingGroup) &&
		slices.EqualFunc(snapshot.PDComponents, other.PDComponents, equalRoutingPDComponent) &&
		slices.EqualFunc(snapshot.PDPipelineScopes, other.PDPipelineScopes, equalRoutingPDPipelineScope) &&
		slices.EqualFunc(snapshot.EPDComponents, other.EPDComponents, equalRoutingEPDComponent) &&
		slices.EqualFunc(snapshot.EPDPipelineScopes, other.EPDPipelineScopes, equalRoutingEPDPipelineScope)
}

func equalScalingModel(left, right Model) bool {
	return left.ServiceGeneration == right.ServiceGeneration && left.ServiceUID == right.ServiceUID &&
		left.Model == right.Model && left.Source == right.Source && left.Revision == right.Revision &&
		left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision &&
		reflect.DeepEqual(left.PrefixCacheIsolation, right.PrefixCacheIsolation) &&
		reflect.DeepEqual(left.RouterPipeline, right.RouterPipeline) && left.Topology == right.Topology &&
		slices.Equal(left.SelectedPoolRevisions, right.SelectedPoolRevisions) &&
		slices.Equal(left.Capabilities, right.Capabilities) &&
		slices.EqualFunc(left.AdmissionTargetSets, right.AdmissionTargetSets, func(left, right []ScalingTarget) bool { return slices.Equal(left, right) })
}

func equalRoutingGroup(left, right Group) bool {
	return left.RouteTargetID == right.RouteTargetID &&
		left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName &&
		MatchingRoutingIdentity(left, right) && ptr.Equal(left.MaxInputTokens, right.MaxInputTokens) &&
		left.Endpoint == right.Endpoint && left.KVScopeID == right.KVScopeID && left.KVLookupScope == right.KVLookupScope &&
		left.DataParallelSize == right.DataParallelSize && slices.Equal(left.Capabilities, right.Capabilities)
}

func equalRoutingPDComponent(left, right PDComponent) bool {
	return left.RouteTargetID == right.RouteTargetID &&
		left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName &&
		left.Role == right.Role && left.PipelineScopeID == right.PipelineScopeID &&
		left.Model == right.Model && left.Source == right.Source && left.Revision == right.Revision &&
		left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision &&
		reflect.DeepEqual(left.PrefixCacheIsolation, right.PrefixCacheIsolation) &&
		ptr.Equal(left.MaxInputTokens, right.MaxInputTokens) &&
		left.ProfileName == right.ProfileName && left.ProfileRevision == right.ProfileRevision &&
		left.Connector == right.Connector && left.Protocol == right.Protocol &&
		left.Endpoint == right.Endpoint && left.PrefillBootstrapEndpoint == right.PrefillBootstrapEndpoint &&
		left.KVScopeID == right.KVScopeID && left.KVLookupScope == right.KVLookupScope &&
		left.DataParallelSize == right.DataParallelSize && slices.Equal(left.Capabilities, right.Capabilities)
}

func equalRoutingPDPipelineScope(left, right PDPipelineScope) bool {
	return left.PipelineScopeID == right.PipelineScopeID &&
		slices.Equal(left.PrefillRouteTargetIDs, right.PrefillRouteTargetIDs) &&
		slices.Equal(left.DecodeRouteTargetIDs, right.DecodeRouteTargetIDs)
}

func equalRoutingEPDComponent(left, right EPDComponent) bool {
	return left.RouteTargetID == right.RouteTargetID &&
		left.ServiceUID == right.ServiceUID && left.PoolUID == right.PoolUID && left.PoolName == right.PoolName &&
		left.Role == right.Role && left.Model == right.Model && left.Source == right.Source && left.Revision == right.Revision &&
		left.Tokenizer == right.Tokenizer && left.TokenizerRevision == right.TokenizerRevision &&
		reflect.DeepEqual(left.PrefixCacheIsolation, right.PrefixCacheIsolation) &&
		ptr.Equal(left.MaxInputTokens, right.MaxInputTokens) && slices.Equal(left.Capabilities, right.Capabilities) &&
		left.Endpoint == right.Endpoint && left.PrefillBootstrapEndpoint == right.PrefillBootstrapEndpoint &&
		left.KVScopeID == right.KVScopeID && left.KVLookupScope == right.KVLookupScope &&
		left.DataParallelSize == right.DataParallelSize
}

func equalRoutingEPDPipelineScope(left, right EPDPipelineScope) bool {
	return left.PipelineScopeID == right.PipelineScopeID &&
		slices.Equal(left.EncoderRouteTargetIDs, right.EncoderRouteTargetIDs) &&
		slices.Equal(left.PrefillRouteTargetIDs, right.PrefillRouteTargetIDs) &&
		slices.Equal(left.DecodeRouteTargetIDs, right.DecodeRouteTargetIDs)
}
