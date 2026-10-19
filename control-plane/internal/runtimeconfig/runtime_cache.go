// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves runtime cache bindings and placement shared by workload controllers.

package runtimeconfig

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
	CacheVolumeName = "runtime-cache"
	CacheClaimEnv   = "FORETOKEN_RUNTIME_CACHE_CLAIM"
	// RuntimeCacheDirectoryAnnotation records the filesystem selected by a prepared claim.
	RuntimeCacheDirectoryAnnotation = "inference.foretoken.io/runtime-cache-directory"
)

// CacheObserverEnv gives preparation and serving processes the same filesystem observer identity.
func CacheObserverEnv(cache inferencev1alpha1.RuntimeCacheBinding, runtimePort int32) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: "FORETOKEN_CACHE_MOUNT_PATH", Value: cache.MountPath},
		{Name: CacheClaimEnv, Value: cache.ClaimName},
		{Name: "FORETOKEN_CACHE_OBSERVATION_PORT", Value: strconv.Itoa(int(CacheObservationPort(runtimePort)))},
	}
}

// PlaceRuntimeCache keeps all consumers of a single-node writable claim together.
// Self-affinity lets the first consumer establish placement; later preparation, serving,
// and frontend Pods follow that node without maintaining a second attachment inventory.
func PlaceRuntimeCache(ctx context.Context, reader client.Reader, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding, pod *corev1.PodTemplateSpec) error {
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

// RuntimeCacheDirectoryBindings authorizes exact directories without granting PV access.
// Directories absent from the map are not authorized.
type RuntimeCacheDirectoryBindings map[string]string

// DirectoryPVC selects prepared storage for cache reconciliation without taking ownership.
func (bindings RuntimeCacheDirectoryBindings) DirectoryPVC(ctx context.Context, reader client.Reader, cache *inferencev1alpha1.RuntimeCache) (*corev1.PersistentVolumeClaim, error) {
	name := bindings[cache.Spec.Directory]
	if name == "" {
		return nil, fmt.Errorf("directory %q has no administrator-prepared claim in namespace %q", cache.Spec.Directory, cache.Namespace)
	}
	return ReadPreparedCachePVC(ctx, reader, cache.Namespace, name)
}

// ReadPreparedCachePVC returns a borrowed filesystem claim without changing its lifecycle.
func ReadPreparedCachePVC(ctx context.Context, reader client.Reader, namespace, name string) (*corev1.PersistentVolumeClaim, error) {
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

// RuntimeCacheProfile selects persistent storage from administrator-prepared directory bindings or defaults.
type RuntimeCacheProfile struct {
	ClaimName         string
	MountPath         string
	DirectoryBindings RuntimeCacheDirectoryBindings
}

// Resolve selects an explicit directory, an existing-claim default, or a managed cache.
func (profile RuntimeCacheProfile) Resolve(ctx context.Context, kubeClient client.Client, namespace string) (*inferencev1alpha1.RuntimeCacheBinding, bool, error) {
	var caches inferencev1alpha1.RuntimeCacheList
	if err := kubeClient.List(ctx, &caches, client.InNamespace(namespace)); err != nil {
		return nil, false, fmt.Errorf("list RuntimeCaches: %w", err)
	}
	directoryRequested := false
	for _, cache := range caches.Items {
		if cache.Spec.Directory != "" {
			directoryRequested = true
			break
		}
	}
	claimName := profile.ClaimName
	var cache *inferencev1alpha1.RuntimeCache
	// An explicit directory selects storage; an existing-claim default must not
	// silently replace it with a different filesystem.
	if claimName == "" || directoryRequested {
		if len(caches.Items) == 0 {
			return nil, true, nil
		}
		if len(caches.Items) > 1 {
			return nil, false, fmt.Errorf("namespace %q has multiple RuntimeCaches", namespace)
		}
		cache = &caches.Items[0]
		if !cache.DeletionTimestamp.IsZero() {
			return nil, true, nil
		}
		if cache.Status.Phase == inferencev1alpha1.RuntimeCachePhaseDegraded {
			return nil, false, nil
		}
		if cache.Status.ObservedGeneration != cache.Generation || cache.Status.ClaimName == "" {
			return nil, false, nil
		}
		claimName = cache.Status.ClaimName
	}
	claim := new(corev1.PersistentVolumeClaim)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: namespace, Name: claimName}, claim); err != nil {
		return nil, false, fmt.Errorf("read runtime cache claim: %w", err)
	}
	directory := ""
	if cache != nil && cache.Spec.Directory != "" {
		if profile.DirectoryBindings[cache.Spec.Directory] != claim.Name {
			return nil, false, fmt.Errorf("directory %q has no matching administrator-prepared claim", cache.Spec.Directory)
		}
		directory = cache.Spec.Directory
	} else if cache == nil {
		for authorized, name := range profile.DirectoryBindings {
			if name == claim.Name {
				directory = authorized
				break
			}
		}
		if directory == "" {
			directory = claim.Annotations[RuntimeCacheDirectoryAnnotation]
		}
	}
	return profile.binding(claim.Name, directory, claim.Annotations[DirectoryOwnerAnnotation])
}

// binding pins the confirmed directory and storage identity for execution and lifecycle consumers.
func (profile RuntimeCacheProfile) binding(claimName, directory, owner string) (*inferencev1alpha1.RuntimeCacheBinding, bool, error) {
	identity, err := ParseStorageIdentity(owner)
	if err != nil {
		return nil, false, fmt.Errorf("runtime cache claim %q: %w", claimName, err)
	}
	return &inferencev1alpha1.RuntimeCacheBinding{
		ClaimName: claimName, MountPath: profile.MountPath,
		Directory: directory, DirectoryOwner: identity,
	}, true, nil
}

// Validate rejects an invalid runtime cache mount path.
func (profile RuntimeCacheProfile) Validate() error {
	if profile.MountPath == "" || profile.MountPath == "/" || !strings.HasPrefix(profile.MountPath, "/") {
		return fmt.Errorf("cache mount path must be an absolute non-root path")
	}
	return nil
}
