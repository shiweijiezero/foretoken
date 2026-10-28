// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Coordinates frontend cache readiness and placement with the selected serving cohorts.
package frontend

import (
	"context"
	"fmt"
	"reflect"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// servingCacheReady preserves selected cohorts' cache bindings while allowing
// frontends to start independently on already-bound shared storage.
func (reconciler *Reconciler) servingCacheReady(ctx context.Context, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding, services []inferencev1alpha1.ModelService) (bool, error) {
	sharedClaimReady := false
	if cache != nil {
		claim, err := runtimeconfig.ReadPreparedCachePVC(ctx, reconciler.Client, namespace, cache.ClaimName)
		if err != nil {
			return false, err
		}
		// An unbound or single-node claim must follow model placement rather than
		// letting a frontend bind it to a node that cannot host the model.
		sharedClaimReady = claim.Status.Phase == corev1.ClaimBound && slices.Contains(claim.Spec.AccessModes, corev1.ReadWriteMany)
	}
	var pools inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(namespace)); err != nil {
		return false, fmt.Errorf("list ModelPools for frontend runtime cache: %w", err)
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(namespace)); err != nil {
		return false, fmt.Errorf("list ModelGroups for frontend runtime cache: %w", err)
	}
	selectedRevision := false
	for serviceIndex := range services {
		service := &services[serviceIndex]
		if !service.DeletionTimestamp.IsZero() {
			continue
		}
		for _, selected := range service.Status.ServingPoolRevisions {
			selectedRevision = true
			var pool *inferencev1alpha1.ModelPool
			for poolIndex := range pools.Items {
				candidate := &pools.Items[poolIndex]
				if candidate.Spec.PoolName == selected.PoolName && string(candidate.UID) == selected.PoolUID && servingconfig.RoutingPoolOwnedBy(candidate, service) {
					pool = candidate
					break
				}
			}
			if pool == nil {
				return false, nil
			}
			matched := false
			for groupIndex := range groups.Items {
				group := &groups.Items[groupIndex]
				if servingconfig.RoutingGroupOwnedBy(group, pool) && group.Spec.Revision == selected.Revision {
					matched = true
					if !reflect.DeepEqual(group.Spec.Artifacts.Cache, cache) || !sharedClaimReady && !servingconfig.RoutingGroupReady(group) {
						return false, nil
					}
				}
			}
			if !matched {
				return false, nil
			}
		}
	}
	return cache == nil || sharedClaimReady || selectedRevision, nil
}

// placeFrontendCache shares the cache placement contract and permits its accelerator-node taint.
func (reconciler *Reconciler) placeFrontendCache(ctx context.Context, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding, pod *corev1.PodTemplateSpec) error {
	if err := runtimeconfig.PlaceRuntimeCache(ctx, reconciler.Client, namespace, cache, pod); err != nil {
		return err
	}
	if pod.Spec.Affinity == nil {
		return nil
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(namespace)); err != nil {
		return err
	}
	for _, group := range groups.Items {
		if binding := group.Spec.Artifacts.Cache; binding != nil && binding.ClaimName == cache.ClaimName {
			for _, toleration := range workload.AcceleratorTolerations(group.Spec.Accelerator.DeviceResourceName) {
				if !slices.Contains(pod.Spec.Tolerations, toleration) {
					pod.Spec.Tolerations = append(pod.Spec.Tolerations, toleration)
				}
			}
		}
	}
	return nil
}

// frontendsForClaim refreshes consumers when a borrowed cache or video claim changes identity.
func (reconciler *Reconciler) frontendsForClaim(ctx context.Context, object client.Object) []reconcile.Request {
	var frontends inferencev1alpha1.FrontendServiceList
	if err := reconciler.List(ctx, &frontends, client.InNamespace(object.GetNamespace())); err != nil {
		ctrl.LoggerFrom(ctx).Error(err, "list frontends for storage claim")
		return nil
	}
	requests := make([]reconcile.Request, 0, len(frontends.Items))
	for index := range frontends.Items {
		frontend := &frontends.Items[index]
		video := frontend.Spec.VideoTasks
		if object.GetName() == reconciler.CacheProfile.ClaimName || video != nil && video.ClaimName == object.GetName() {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(frontend)})
		}
	}
	return requests
}
