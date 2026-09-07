// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the controller-owned persistent runtime cache profile.

package controllers

import (
	"context"
	"fmt"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const runtimeCacheVolumeName = "runtime-cache"

func runtimeCacheObservationPort(runtimePort int32) int32 {
	if runtimePort < 65535 {
		return runtimePort + 1
	}
	return runtimePort - 1
}

// RuntimeSourceProfile configures optional source access for the runtime adapter.
type RuntimeSourceProfile struct {
	Endpoint        string
	TokenSecretName string
	TokenSecretKey  string
}

// RuntimeSource returns the immutable source contract copied into a ModelPool generation.
func (profile RuntimeSourceProfile) RuntimeSource() *inferencev1alpha1.RuntimeSourceAccess {
	if profile.Endpoint == "" && profile.TokenSecretName == "" {
		return nil
	}
	return &inferencev1alpha1.RuntimeSourceAccess{Endpoint: profile.Endpoint, TokenSecretName: profile.TokenSecretName, TokenSecretKey: profile.TokenSecretKey}
}

// Validate rejects incomplete source credential settings.
func (profile RuntimeSourceProfile) Validate() error {
	if (profile.TokenSecretName == "") != (profile.TokenSecretKey == "") {
		return fmt.Errorf("runtime source Secret name and key must be configured together")
	}
	return nil
}

// RuntimeCacheProfile configures the cache mount path and an optional existing-claim override.
type RuntimeCacheProfile struct {
	ClaimName string
	MountPath string
}

// Resolve selects the configured existing claim or the single usable managed cache in a namespace.
func (profile RuntimeCacheProfile) Resolve(ctx context.Context, kubeClient client.Client, namespace string) (*inferencev1alpha1.RuntimeCacheBinding, bool, error) {
	if profile.ClaimName != "" {
		return &inferencev1alpha1.RuntimeCacheBinding{ClaimName: profile.ClaimName, MountPath: profile.MountPath}, true, nil
	}
	var caches inferencev1alpha1.RuntimeCacheList
	if err := kubeClient.List(ctx, &caches, client.InNamespace(namespace)); err != nil {
		return nil, false, fmt.Errorf("list RuntimeCaches: %w", err)
	}
	if len(caches.Items) == 0 {
		return nil, true, nil
	}
	if len(caches.Items) > 1 {
		return nil, false, fmt.Errorf("namespace %q has multiple RuntimeCaches", namespace)
	}
	cache := &caches.Items[0]
	if !cache.DeletionTimestamp.IsZero() || cache.Status.Phase == inferencev1alpha1.RuntimeCachePhaseDegraded || cache.Status.Phase == inferencev1alpha1.RuntimeCachePhaseTerminating {
		return nil, false, nil
	}
	if cache.Status.ObservedGeneration != cache.Generation || cache.Status.ClaimName == "" {
		return nil, false, nil
	}
	binding := &inferencev1alpha1.RuntimeCacheBinding{ClaimName: cache.Status.ClaimName, MountPath: profile.MountPath}
	if cache.Spec.MaxSize != "" {
		bytes, err := runtimeCacheQuantityBytes(cache.Spec.InitialSize, "initialSize")
		if err != nil {
			return nil, false, fmt.Errorf("RuntimeCache %q: %w", cache.Name, err)
		}
		binding.MinimumAvailableBytes = bytes
	}
	return binding, true, nil
}

// Validate rejects an invalid runtime cache mount path.
func (profile RuntimeCacheProfile) Validate() error {
	if profile.MountPath == "" || profile.MountPath == "/" || !strings.HasPrefix(profile.MountPath, "/") {
		return fmt.Errorf("cache mount path must be an absolute non-root path")
	}
	return nil
}
