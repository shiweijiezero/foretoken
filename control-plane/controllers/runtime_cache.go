// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the controller-owned persistent runtime cache profile.

package controllers

import (
	"context"
	"fmt"
	"strconv"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	runtimeCacheVolumeName = "runtime-cache"
	runtimeCacheClaimEnv   = "FORETOKEN_RUNTIME_CACHE_CLAIM"
)

func runtimeCacheObservationPort(runtimePort int32) int32 {
	if runtimePort < 65535 {
		return runtimePort + 1
	}
	return runtimePort - 1
}

// runtimeCacheObserverEnv gives preparation and serving processes the same filesystem observer identity.
func runtimeCacheObserverEnv(cache inferencev1alpha1.RuntimeCacheBinding, runtimePort int32) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: "FORETOKEN_CACHE_MOUNT_PATH", Value: cache.MountPath},
		{Name: runtimeCacheClaimEnv, Value: cache.ClaimName},
		{Name: "FORETOKEN_CACHE_OBSERVATION_PORT", Value: strconv.Itoa(int(runtimeCacheObservationPort(runtimePort)))},
		{Name: "FORETOKEN_POD_UID", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.uid"}}},
	}
}

// placeRuntimeCache keeps all consumers of a single-node writable claim together.
// Self-affinity lets the first consumer establish placement; later preparation, serving,
// and frontend Pods follow that node without maintaining a second attachment inventory.
func placeRuntimeCache(ctx context.Context, reader client.Reader, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding, pod *corev1.PodTemplateSpec) error {
	if cache == nil {
		return nil
	}
	claim := new(corev1.PersistentVolumeClaim)
	if err := reader.Get(ctx, client.ObjectKey{Namespace: namespace, Name: cache.ClaimName}, claim); err != nil {
		return fmt.Errorf("read runtime cache access modes: %w", err)
	}
	for _, mode := range claim.Spec.AccessModes {
		if mode == corev1.ReadWriteMany {
			return nil
		}
	}
	const claimLabel = "inference.foretoken.io/runtime-cache-claim"
	if pod.Labels == nil {
		pod.Labels = make(map[string]string)
	}
	pod.Labels[claimLabel] = string(claim.UID)
	if pod.Spec.Affinity == nil {
		pod.Spec.Affinity = &corev1.Affinity{}
	}
	pod.Spec.Affinity.PodAffinity = &corev1.PodAffinity{
		RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{{
			TopologyKey:   corev1.LabelHostname,
			LabelSelector: &metav1.LabelSelector{MatchLabels: map[string]string{claimLabel: string(claim.UID)}},
		}},
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
	if !cache.DeletionTimestamp.IsZero() {
		return nil, true, nil
	}
	if cache.Status.Phase == inferencev1alpha1.RuntimeCachePhaseDegraded {
		return nil, false, nil
	}
	if cache.Status.ObservedGeneration != cache.Generation || cache.Status.ClaimName == "" {
		return nil, false, nil
	}
	return &inferencev1alpha1.RuntimeCacheBinding{ClaimName: cache.Status.ClaimName, MountPath: profile.MountPath}, true, nil
}

// Validate rejects an invalid runtime cache mount path.
func (profile RuntimeCacheProfile) Validate() error {
	if profile.MountPath == "" || profile.MountPath == "/" || !strings.HasPrefix(profile.MountPath, "/") {
		return fmt.Errorf("cache mount path must be an absolute non-root path")
	}
	return nil
}
