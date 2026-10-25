// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Freezes prepared Draft Group endpoints into each Target's execution cohort.
package model

import (
	"bytes"
	"cmp"
	"context"
	"fmt"
	"slices"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// resolveDraftPools binds a Target to concrete, prepared same-service Draft instances.
// Pending dependencies preserve existing Target executions without acquiring another checkpoint.
func (reconciler *PoolReconciler) resolveDraftPools(ctx context.Context, pool *api.ModelPool, service *api.ModelService, target *resolver.ModelGroupTemplate) (bool, error) {
	if target.SpeculationRole != api.SpeculationRoleTarget {
		return true, nil
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var pools api.ModelPoolList
	if err := reader.List(ctx, &pools, client.InNamespace(pool.Namespace)); err != nil {
		return false, err
	}
	var groups api.ModelGroupList
	if err := reader.List(ctx, &groups, client.InNamespace(pool.Namespace)); err != nil {
		return false, err
	}
	// A Draft can retire between dependency resolution and Target creation.
	// Retire that unreachable Target even when no replacement Draft is ready yet.
	for index := range groups.Items {
		group := &groups.Items[index]
		if !servingconfig.ModelGroupOwnedBy(group, pool) || group.Spec.SpeculationRole != api.SpeculationRoleTarget {
			continue
		}
		binding := group.Spec.SelectedDraftGroup()
		if binding == nil {
			continue
		}
		missing := !slices.ContainsFunc(groups.Items, func(draft api.ModelGroup) bool {
			return draft.Name == binding.GroupRef.Name && string(draft.UID) == binding.GroupRef.UID && draft.Spec.ModelPoolRef == binding.PoolRef && draft.Spec.Revision == binding.Revision
		})
		if missing {
			if err := retireModelGroup(ctx, reconciler.Client, group); err != nil {
				return false, err
			}
		}
	}
	for _, name := range pool.Spec.Template.DraftPools {
		var draft *api.ModelPool
		for index := range pools.Items {
			candidate := &pools.Items[index]
			if servingconfig.RoutingPoolOwnedBy(candidate, service) && candidate.Spec.PoolName == name {
				draft = candidate
				break
			}
		}
		if draft == nil || draft.Spec.Template.SpeculationRole != api.SpeculationRoleDraft || draft.Spec.DesiredGroups == 0 || draft.Status.ObservedGeneration != draft.Generation || draft.Status.PreparedRevision == "" {
			return false, nil
		}
		// Pool updates arrive separately. Bind peers from the same executable
		// selection even when the Target is reconciled before its Draft update.
		application := draft.Spec.Template.Application
		if application == nil || application.SourceRevision != pool.Spec.Template.Application.SourceRevision || application.DeploymentRevision != pool.Spec.Template.Application.DeploymentRevision {
			return false, nil
		}
		found := false
		for index := range groups.Items {
			group := &groups.Items[index]
			if !servingconfig.RoutingGroupOwnedBy(group, draft) || group.Spec.Revision != draft.Status.PreparedRevision || group.Spec.Ordinal >= draft.Spec.DesiredGroups || !servingconfig.RoutingGroupReady(group) {
				continue
			}
			if group.Spec.SpeculationRole != api.SpeculationRoleDraft || runtimeconfig.ProjectModelArtifacts(group.Spec.Artifacts) != runtimeconfig.ResolveModelArtifacts(pool.Spec.Template) || !bytes.Equal(group.Spec.Runtime.EngineArgs["speculative-config"].Raw, target.Runtime.EngineArgs["speculative-config"].Raw) {
				return false, fmt.Errorf("Draft Pool %q is not compatible with this Target's model and native speculative configuration", name)
			}
			target.DraftGroups = append(target.DraftGroups, api.DraftGroupBinding{
				PoolRef:  api.LocalObjectReference{Name: draft.Name, UID: string(draft.UID)},
				Revision: group.Spec.Revision,
				GroupRef: api.LocalObjectReference{Name: group.Name, UID: string(group.UID)},
				Endpoint: workload.ModelGroupEndpoint(group, group.Spec.Runtime.Port),
			})
			found = true
		}
		if !found {
			return false, nil
		}
	}
	slices.SortFunc(target.DraftGroups, func(left, right api.DraftGroupBinding) int {
		if order := cmp.Compare(left.PoolRef.Name, right.PoolRef.Name); order != 0 {
			return order
		}
		return cmp.Compare(left.GroupRef.Name, right.GroupRef.Name)
	})
	return len(target.DraftGroups) > 0, nil
}

// poolsForDraftPool refreshes Target bindings when a referenced Draft's prepared cohort changes.
func (reconciler *PoolReconciler) poolsForDraftPool(ctx context.Context, object client.Object) []reconcile.Request {
	draft := object.(*api.ModelPool)
	var pools api.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(draft.Namespace)); err != nil {
		return nil
	}
	var requests []reconcile.Request
	for index := range pools.Items {
		pool := &pools.Items[index]
		if pool.Spec.ModelServiceRef == draft.Spec.ModelServiceRef && slices.Contains(pool.Spec.Template.DraftPools, draft.Spec.PoolName) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(pool)})
		}
	}
	return requests
}

// poolsForDraftGroup wakes dependency owners as Draft membership or Target consumers change.
func (reconciler *PoolReconciler) poolsForDraftGroup(ctx context.Context, object client.Object) []reconcile.Request {
	group := object.(*api.ModelGroup)
	var requests []reconcile.Request
	if binding := group.Spec.SelectedDraftGroup(); binding != nil {
		requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKey{Namespace: group.Namespace, Name: binding.PoolRef.Name}})
	}
	if group.Spec.SpeculationRole == api.SpeculationRoleDraft {
		pool := new(api.ModelPool)
		if err := reconciler.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: group.Spec.ModelPoolRef.Name}, pool); err == nil && string(pool.UID) == group.Spec.ModelPoolRef.UID {
			requests = append(requests, reconciler.poolsForDraftPool(ctx, pool)...)
		}
	}
	return requests
}
