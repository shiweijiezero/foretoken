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
