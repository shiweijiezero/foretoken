// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Coordinates retained Draft consumers and their service-local transport permissions.
package model

import (
	"context"
	"slices"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// draftBindingContains matches the Draft instance selected for this Target process.
func draftBindingContains(target, draft *api.ModelGroup) bool {
	binding := target.Spec.SelectedDraftGroup()
	return binding != nil && binding.PoolRef.UID == draft.Spec.ModelPoolRef.UID && binding.Revision == draft.Spec.Revision && binding.GroupRef.Name == draft.Name && binding.GroupRef.UID == string(draft.UID)
}

// retireDraftConsumers drains Target owners before the shared Draft closes its own admission.
// Its drain budget starts only after these consumers have released their executions.
func (reconciler *GroupReconciler) retireDraftConsumers(ctx context.Context, draft *api.ModelGroup) (bool, error) {
	if draft.Spec.SpeculationRole != api.SpeculationRoleDraft {
		return false, nil
	}
	pool, err := reconciler.owningModelPool(ctx, draft)
	if err != nil {
		return false, err
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var groups api.ModelGroupList
	if err := reader.List(ctx, &groups, client.InNamespace(draft.Namespace)); err != nil {
		return false, err
	}
	pending := false
	for index := range groups.Items {
		target := &groups.Items[index]
		if target.Spec.SpeculationRole != api.SpeculationRoleTarget || !draftBindingContains(target, draft) {
			continue
		}
		targetPool, err := reconciler.owningModelPool(ctx, target)
		if err != nil {
			return false, err
		}
		if targetPool.Spec.ModelServiceRef != pool.Spec.ModelServiceRef || !workload.ControllerOwnerMatches(targetPool, api.GroupVersion.String(), "ModelService", pool.Spec.ModelServiceRef.Name, types.UID(pool.Spec.ModelServiceRef.UID)) {
			continue
		}
		pending = true
		if err := retireModelGroup(ctx, reconciler.Client, target); err != nil {
			return false, err
		}
	}
	return pending, nil
}

// draftPeerIngress restricts native transport TCP channels to bound peer Groups.
// Both directions use the same frozen dependency, including Targets already draining.
func (reconciler *GroupReconciler) draftPeerIngress(ctx context.Context, group *api.ModelGroup, pool *api.ModelPool) ([]networkingv1.NetworkPolicyIngressRule, error) {
	if group.Spec.SpeculationRole == "" {
		return nil, nil
	}
	var groups api.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(group.Namespace)); err != nil {
		return nil, err
	}
	var pools api.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(group.Namespace)); err != nil {
		return nil, err
	}
	var peers []networkingv1.NetworkPolicyPeer
	for index := range groups.Items {
		peer := &groups.Items[index]
		linked := group.Spec.SpeculationRole == api.SpeculationRoleDraft && draftBindingContains(peer, group) || group.Spec.SpeculationRole == api.SpeculationRoleTarget && draftBindingContains(group, peer)
		if !linked {
			continue
		}
		owned := slices.ContainsFunc(pools.Items, func(candidate api.ModelPool) bool {
			return candidate.Spec.ModelServiceRef == pool.Spec.ModelServiceRef && workload.ControllerOwnerMatches(&candidate, api.GroupVersion.String(), "ModelService", pool.Spec.ModelServiceRef.Name, types.UID(pool.Spec.ModelServiceRef.UID)) && servingconfig.ModelGroupOwnedBy(peer, &candidate)
		})
		if !owned {
			continue
		}
		peers = append(peers, networkingv1.NetworkPolicyPeer{PodSelector: &metav1.LabelSelector{MatchLabels: map[string]string{
			api.DeploymentUIDLabel:        group.Labels[api.DeploymentUIDLabel],
			workload.ModelServiceUIDLabel: pool.Spec.ModelServiceRef.UID,
			workload.ModelGroupUIDLabel:   string(peer.UID),
		}}})
	}
	if len(peers) == 0 {
		return nil, nil
	}
	tcp := corev1.ProtocolTCP
	return []networkingv1.NetworkPolicyIngressRule{{From: peers, Ports: []networkingv1.NetworkPolicyPort{{Protocol: &tcp}}}}, nil
}

// modelGroupsForDraftPeer updates the reciprocal policy when a Target binding is materialized or retired.
func (reconciler *GroupReconciler) modelGroupsForDraftPeer(ctx context.Context, object client.Object) []reconcile.Request {
	group := object.(*api.ModelGroup)
	if group.Spec.SpeculationRole == "" {
		return nil
	}
	var groups api.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(group.Namespace)); err != nil {
		return nil
	}
	var requests []reconcile.Request
	for index := range groups.Items {
		peer := &groups.Items[index]
		if draftBindingContains(group, peer) || draftBindingContains(peer, group) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(peer)})
		}
	}
	return requests
}
