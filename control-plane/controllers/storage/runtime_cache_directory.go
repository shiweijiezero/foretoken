// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Observes prepared cache bindings without adopting their PVC lifecycle.

package storage

import (
	"context"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// preparedClaimRequests wakes consumers of default claims and authorized directory bindings.
func (reconciler *RuntimeCacheReconciler) preparedClaimRequests(ctx context.Context, claim client.Object) []reconcile.Request {
	matched := claim.GetName() == reconciler.Profile.ClaimName
	for _, name := range reconciler.Profile.DirectoryBindings {
		if name == claim.GetName() {
			matched = true
			break
		}
	}
	if !matched {
		return nil
	}
	var caches inferencev1alpha1.RuntimeCacheList
	if err := reconciler.List(ctx, &caches, client.InNamespace(claim.GetNamespace())); err != nil {
		ctrl.LoggerFrom(ctx).Error(err, "list prepared cache consumers")
		return nil
	}
	requests := make([]reconcile.Request, 0, len(caches.Items))
	for _, cache := range caches.Items {
		name := reconciler.Profile.ClaimName
		if cache.Spec.Directory != "" {
			name = reconciler.Profile.DirectoryBindings[cache.Spec.Directory]
		}
		if name == claim.GetName() {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&cache)})
		}
	}
	return requests
}
