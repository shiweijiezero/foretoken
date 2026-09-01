// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Aggregates controller-private ModelGroup lifecycle for ModelService scaling.

package controllers

import (
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"k8s.io/apimachinery/pkg/api/meta"
)

// modelPoolReplicaState counts actual Groups only. Requested capacity is assigned by the
// caller, then any not-yet-materialized requested ordinals are recorded as Pending.
func modelPoolReplicaState(service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) core.ReplicaState {
	var replicaState core.ReplicaState
	if pool == nil {
		return replicaState
	}
	for index := range groups {
		group := &groups[index]
		if !modelGroupOwnedByPool(group, pool) {
			continue
		}
		addGroupLifecycle(&replicaState, group)
		if group.Spec.Revision == serviceServingRevision(service, pool) && routingGroupReady(group) {
			replicaState.RoutableReplicas++
		}
	}
	return replicaState
}

// epdPipelineReplicaState counts one unit only when its encoder, prefill, and decode
// members form an ordinal-complete active revision triplet. This deliberately does
// not apply to P/D: P and D retain their independent Pool scaling targets.
func epdPipelineReplicaState(service *inferencev1alpha1.ModelService, pools map[string]*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, requested int32) core.ReplicaState {
	byRoleOrdinal := map[inferencev1alpha1.ModelRole]map[int32]*inferencev1alpha1.ModelGroup{
		inferencev1alpha1.ModelRoleEncoder: {},
		inferencev1alpha1.ModelRolePrefill: {},
		inferencev1alpha1.ModelRoleDecode:  {},
	}
	for index := range groups {
		group := &groups[index]
		pool := poolForEPDGroup(pools, group)
		if pool == nil || group.Spec.Revision != serviceServingRevision(service, pool) || group.Spec.Ordinal >= requested {
			continue
		}
		roleGroups := byRoleOrdinal[group.Spec.Role]
		if _, found := roleGroups[group.Spec.Ordinal]; found {
			// A duplicate ordinal cannot represent a complete, safely routeable triplet.
			roleGroups[group.Spec.Ordinal] = nil
			continue
		}
		roleGroups[group.Spec.Ordinal] = group
	}

	var replicaState core.ReplicaState
	for ordinal := int32(0); ordinal < requested; ordinal++ {
		encoder := byRoleOrdinal[inferencev1alpha1.ModelRoleEncoder][ordinal]
		prefill := byRoleOrdinal[inferencev1alpha1.ModelRolePrefill][ordinal]
		decode := byRoleOrdinal[inferencev1alpha1.ModelRoleDecode][ordinal]
		if encoder == nil || prefill == nil || decode == nil {
			replicaState.PendingReplicas++
			continue
		}
		addTripletLifecycle(&replicaState, encoder, prefill, decode)
	}
	return replicaState
}

func poolForEPDGroup(pools map[string]*inferencev1alpha1.ModelPool, group *inferencev1alpha1.ModelGroup) *inferencev1alpha1.ModelPool {
	if _, expected := map[inferencev1alpha1.ModelRole]struct{}{
		inferencev1alpha1.ModelRoleEncoder: {}, inferencev1alpha1.ModelRolePrefill: {}, inferencev1alpha1.ModelRoleDecode: {},
	}[group.Spec.Role]; !expected {
		return nil
	}
	for _, pool := range pools {
		if pool != nil && pool.Spec.Template.Role == group.Spec.Role && modelGroupOwnedByPool(group, pool) {
			return pool
		}
	}
	return nil
}

func modelGroupOwnedByPool(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) bool {
	return group != nil && pool != nil &&
		group.Spec.ModelPoolRef.Name == pool.Name && group.Spec.ModelPoolRef.UID == string(pool.UID) &&
		routingControllerOwnerMatches(group, inferencev1alpha1.GroupVersion.String(), "ModelPool", pool.Name, pool.UID)
}

func addGroupLifecycle(replicaState *core.ReplicaState, group *inferencev1alpha1.ModelGroup) {
	if group.DeletionTimestamp.IsZero() && group.Status.Phase == inferencev1alpha1.ModelGroupPhaseReady && routingGroupReady(group) {
		replicaState.ReadyReplicas++
		return
	}
	addLifecycle(replicaState, group)
}

func addTripletLifecycle(replicaState *core.ReplicaState, groups ...*inferencev1alpha1.ModelGroup) {
	allReady := true
	for _, group := range groups {
		allReady = allReady && group.DeletionTimestamp.IsZero() && group.Status.Phase == inferencev1alpha1.ModelGroupPhaseReady && routingGroupReady(group)
	}
	if allReady {
		replicaState.ReadyReplicas++
		replicaState.RoutableReplicas++
		return
	}
	for _, group := range groups {
		if !group.DeletionTimestamp.IsZero() {
			addLifecycle(replicaState, group)
			return
		}
	}
	for _, group := range groups {
		if group.Status.Phase == inferencev1alpha1.ModelGroupPhaseFailed {
			replicaState.FailedReplicas++
			replicaState.Transitioning = true
			return
		}
	}
	for _, group := range groups {
		if group.Status.Phase == inferencev1alpha1.ModelGroupPhaseProvisioning {
			replicaState.ProvisioningReplicas++
			replicaState.Transitioning = true
			return
		}
	}
	replicaState.PendingReplicas++
	replicaState.Transitioning = true
}

func addLifecycle(replicaState *core.ReplicaState, group *inferencev1alpha1.ModelGroup) {
	if !group.DeletionTimestamp.IsZero() {
		if meta.IsStatusConditionTrue(group.Status.Conditions, conditionDrained) {
			replicaState.TerminatingReplicas++
		} else {
			replicaState.DrainingReplicas++
		}
		replicaState.Transitioning = true
		return
	}
	switch group.Status.Phase {
	case inferencev1alpha1.ModelGroupPhaseProvisioning:
		replicaState.ProvisioningReplicas++
	case inferencev1alpha1.ModelGroupPhaseFailed:
		replicaState.FailedReplicas++
	case inferencev1alpha1.ModelGroupPhaseDraining:
		replicaState.DrainingReplicas++
	case inferencev1alpha1.ModelGroupPhaseTerminating:
		replicaState.TerminatingReplicas++
	default:
		// Empty phase is deliberately Pending; desired capacity never implies readiness.
		replicaState.PendingReplicas++
	}
	replicaState.Transitioning = true
}

func finalizeReplicaState(replicaState *core.ReplicaState) {
	observed := replicaState.ReadyReplicas + replicaState.PendingReplicas + replicaState.ProvisioningReplicas + replicaState.DrainingReplicas + replicaState.TerminatingReplicas + replicaState.FailedReplicas
	if observed < replicaState.RequestedReplicas {
		replicaState.PendingReplicas += replicaState.RequestedReplicas - observed
		replicaState.Transitioning = true
	}
}
