// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Aggregates controller-private ModelGroup lifecycle for ModelService scaling.

package controllers

import (
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"k8s.io/apimachinery/pkg/api/meta"
)

// modelPoolCapacity counts actual Groups only. Requested capacity is assigned by the
// caller, then any not-yet-materialized requested ordinals are recorded as Pending.
func modelPoolCapacity(service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) core.CapacityState {
	var capacity core.CapacityState
	if pool == nil {
		return capacity
	}
	for index := range groups {
		group := &groups[index]
		if !modelGroupOwnedByPool(group, pool) {
			continue
		}
		addGroupLifecycle(&capacity, group)
		if group.Spec.Revision == serviceServingRevision(service, pool) && routingGroupReady(group) {
			capacity.RoutableGroups++
		}
	}
	return capacity
}

// epdPipelineScopeCapacity counts one unit only when its encoder, prefill, and decode
// members form an ordinal-complete active revision triplet. This deliberately does
// not apply to P/D: P and D retain their independent Pool scaling targets.
func epdPipelineScopeCapacity(service *inferencev1alpha1.ModelService, pools map[string]*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, requested int32) core.CapacityState {
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

	var capacity core.CapacityState
	for ordinal := int32(0); ordinal < requested; ordinal++ {
		encoder := byRoleOrdinal[inferencev1alpha1.ModelRoleEncoder][ordinal]
		prefill := byRoleOrdinal[inferencev1alpha1.ModelRolePrefill][ordinal]
		decode := byRoleOrdinal[inferencev1alpha1.ModelRoleDecode][ordinal]
		if encoder == nil || prefill == nil || decode == nil {
			capacity.PendingGroups++
			continue
		}
		addTripletLifecycle(&capacity, encoder, prefill, decode)
	}
	return capacity
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

func addGroupLifecycle(capacity *core.CapacityState, group *inferencev1alpha1.ModelGroup) {
	if group.DeletionTimestamp.IsZero() && group.Status.Phase == inferencev1alpha1.ModelGroupPhaseReady && routingGroupReady(group) {
		capacity.ReadyGroups++
		return
	}
	addLifecycle(capacity, group)
}

func addTripletLifecycle(capacity *core.CapacityState, groups ...*inferencev1alpha1.ModelGroup) {
	allReady := true
	for _, group := range groups {
		allReady = allReady && group.DeletionTimestamp.IsZero() && group.Status.Phase == inferencev1alpha1.ModelGroupPhaseReady && routingGroupReady(group)
	}
	if allReady {
		capacity.ReadyGroups++
		capacity.RoutableGroups++
		return
	}
	for _, group := range groups {
		if !group.DeletionTimestamp.IsZero() {
			addLifecycle(capacity, group)
			return
		}
	}
	for _, group := range groups {
		if group.Status.Phase == inferencev1alpha1.ModelGroupPhaseFailed {
			capacity.FailedGroups++
			capacity.Transitioning = true
			return
		}
	}
	for _, group := range groups {
		if group.Status.Phase == inferencev1alpha1.ModelGroupPhaseProvisioning {
			capacity.ProvisioningGroups++
			capacity.Transitioning = true
			return
		}
	}
	capacity.PendingGroups++
	capacity.Transitioning = true
}

func addLifecycle(capacity *core.CapacityState, group *inferencev1alpha1.ModelGroup) {
	if !group.DeletionTimestamp.IsZero() {
		if meta.IsStatusConditionTrue(group.Status.Conditions, conditionDrained) {
			capacity.TerminatingGroups++
		} else {
			capacity.DrainingGroups++
		}
		capacity.Transitioning = true
		return
	}
	switch group.Status.Phase {
	case inferencev1alpha1.ModelGroupPhaseProvisioning:
		capacity.ProvisioningGroups++
	case inferencev1alpha1.ModelGroupPhaseFailed:
		capacity.FailedGroups++
	case inferencev1alpha1.ModelGroupPhaseDraining:
		capacity.DrainingGroups++
	case inferencev1alpha1.ModelGroupPhaseTerminating:
		capacity.TerminatingGroups++
	default:
		// Empty phase is deliberately Pending; desired capacity never implies readiness.
		capacity.PendingGroups++
	}
	capacity.Transitioning = true
}

func finalizeCapacity(capacity *core.CapacityState) {
	observed := capacity.ReadyGroups + capacity.PendingGroups + capacity.ProvisioningGroups + capacity.DrainingGroups + capacity.TerminatingGroups + capacity.FailedGroups
	if observed < capacity.RequestedGroups {
		capacity.PendingGroups += capacity.RequestedGroups - observed
		capacity.Transitioning = true
	}
}
