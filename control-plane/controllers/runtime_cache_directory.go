// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves administrator-selected cache storage without taking ownership of its claims.

package controllers

import (
	"context"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

// RuntimeCacheDirectoryBindings authorizes exact directories without granting PV access.
// Directories absent from the map are not authorized.
type RuntimeCacheDirectoryBindings map[string]string

// directoryPVC selects prepared storage without taking ownership of its lifecycle.
func (bindings RuntimeCacheDirectoryBindings) directoryPVC(ctx context.Context, reader client.Reader, cache *inferencev1alpha1.RuntimeCache) (*corev1.PersistentVolumeClaim, error) {
	name := bindings[cache.Spec.Directory]
	if name == "" {
		return nil, fmt.Errorf("directory %q has no administrator-prepared claim in namespace %q", cache.Spec.Directory, cache.Namespace)
	}
	return readPreparedCachePVC(ctx, reader, cache.Namespace, name)
}

// readPreparedCachePVC verifies a borrowed filesystem claim without changing its lifecycle.
func readPreparedCachePVC(ctx context.Context, reader client.Reader, namespace, name string) (*corev1.PersistentVolumeClaim, error) {
	claim := new(corev1.PersistentVolumeClaim)
	if err := reader.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, claim); err != nil {
		return nil, fmt.Errorf("read prepared runtime cache claim %q: %w", name, err)
	}
	if !claim.DeletionTimestamp.IsZero() {
		return nil, fmt.Errorf("prepared runtime cache claim %q is deleting", name)
	}
	if claim.Spec.VolumeMode != nil && *claim.Spec.VolumeMode != corev1.PersistentVolumeFilesystem {
		return nil, fmt.Errorf("prepared runtime cache claim %q requires Filesystem volume mode", name)
	}
	return claim, nil
}

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
