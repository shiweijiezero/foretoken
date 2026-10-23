// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves runtime cache bindings and placement shared by workload controllers.

package runtimeconfig

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	volumehelpers "k8s.io/component-helpers/storage/volume"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const (
	CacheVolumeName = "runtime-cache"
	CacheClaimEnv   = "FORETOKEN_RUNTIME_CACHE_CLAIM"
	// RuntimeCacheClaimLabel binds co-location and shared consumer reads to one PVC UID.
	RuntimeCacheClaimLabel = "inference.foretoken.io/runtime-cache-claim"
	// RuntimeCacheDirectoryAnnotation records the filesystem selected by a prepared claim.
	RuntimeCacheDirectoryAnnotation = "inference.foretoken.io/runtime-cache-directory"
	// RuntimeCacheVolumeTopologyAnnotation holds the platform-observed binding and native placement constraint.
	RuntimeCacheVolumeTopologyAnnotation = "inference.foretoken.io/runtime-cache-volume-topology"
)

// RuntimeCacheVolumeTopology lets namespace consumers read bound-volume placement
// published by the platform owner on their PVC without receiving PV access.
type RuntimeCacheVolumeTopology struct {
	ClaimUID     types.UID                  `json:"claimUID"`
	VolumeName   string                     `json:"volumeName"`
	NodeAffinity *corev1.VolumeNodeAffinity `json:"nodeAffinity"`
}

// RuntimeCacheDirectoryBindings authorizes exact directories without granting PV access.
// Directories absent from the map are not authorized.
type RuntimeCacheDirectoryBindings map[string]string

// RuntimeCacheProfile selects persistent storage from administrator-prepared directory bindings or defaults.
type RuntimeCacheProfile struct {
	ClaimName         string
	MountPath         string
	DirectoryBindings RuntimeCacheDirectoryBindings
}

// Resolve selects an explicit directory, an existing-claim default, or a managed cache.
func (profile RuntimeCacheProfile) Resolve(ctx context.Context, kubeClient client.Reader, namespace string) (*inferencev1alpha1.RuntimeCacheBinding, bool, error) {
	var caches inferencev1alpha1.RuntimeCacheList
	if err := kubeClient.List(ctx, &caches, client.InNamespace(namespace)); err != nil {
		return nil, false, fmt.Errorf("list RuntimeCaches: %w", err)
	}
	claimName := profile.ClaimName
	var cache *inferencev1alpha1.RuntimeCache
	// RuntimeCache reconciliation owns explicit storage selection. The profile's
	// prepared claim applies only when no cache object selects storage.
	if len(caches.Items) != 0 {
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
	if claimName == "" {
		return nil, true, nil
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
	} else if cache == nil || cache.Spec.InitialSize == "" {
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

// Validate rejects an invalid runtime cache mount path.
func (profile RuntimeCacheProfile) Validate() error {
	if profile.MountPath == "" || profile.MountPath == "/" || !strings.HasPrefix(profile.MountPath, "/") {
		return fmt.Errorf("cache mount path must be an absolute non-root path")
	}
	return nil
}

// PlaceRuntimeCache keeps all consumers of a single-node writable claim together.
// Self-affinity lets the first consumer establish placement; later preparation, serving,
// and frontend Pods follow that node without maintaining a second attachment inventory.
func PlaceRuntimeCache(ctx context.Context, reader client.Reader, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding, pod *corev1.PodTemplateSpec) error {
	claim, err := runtimeCacheClaim(ctx, reader, namespace, cache)
	if err != nil || claim == nil || slices.Contains(claim.Spec.AccessModes, corev1.ReadWriteMany) {
		return err
	}
	if pod.Labels == nil {
		pod.Labels = make(map[string]string)
	}
	pod.Labels[RuntimeCacheClaimLabel] = string(claim.UID)
	if pod.Spec.Affinity == nil {
		pod.Spec.Affinity = &corev1.Affinity{}
	}
	pod.Spec.Affinity.PodAffinity = &corev1.PodAffinity{
		RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{{
			TopologyKey:   corev1.LabelHostname,
			LabelSelector: &metav1.LabelSelector{MatchLabels: map[string]string{RuntimeCacheClaimLabel: string(claim.UID)}},
		}},
	}
	return nil
}

// RuntimeCacheNodes restricts device selection to observed volume topology and existing consumers.
// Unbound claims remain with the scheduler; bound claims wait for the platform's placement observation.
func RuntimeCacheNodes(ctx context.Context, reader client.Reader, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding, nodes []corev1.Node) ([]corev1.Node, error) {
	claim, err := runtimeCacheClaim(ctx, reader, namespace, cache)
	if err != nil || claim == nil {
		return nodes, err
	}
	if claim.Status.Phase == corev1.ClaimLost {
		return nil, fmt.Errorf("runtime cache claim %q has lost its volume", claim.Name)
	}
	if claim.Status.Phase == corev1.ClaimBound {
		encoded := claim.Annotations[RuntimeCacheVolumeTopologyAnnotation]
		if encoded == "" {
			return nil, fmt.Errorf("runtime cache claim %q is waiting for bound-volume topology", claim.Name)
		}
		var topology RuntimeCacheVolumeTopology
		if err := json.Unmarshal([]byte(encoded), &topology); err != nil {
			return nil, fmt.Errorf("read runtime cache volume topology: %w", err)
		}
		if topology.ClaimUID != claim.UID || topology.VolumeName != claim.Spec.VolumeName {
			return nil, fmt.Errorf("runtime cache claim %q is waiting for its current volume binding to be observed", claim.Name)
		}
		volume := &corev1.PersistentVolume{Spec: corev1.PersistentVolumeSpec{NodeAffinity: topology.NodeAffinity}}
		nodes = slices.DeleteFunc(slices.Clone(nodes), func(node corev1.Node) bool {
			return volumehelpers.CheckNodeAffinity(volume, node.Labels) != nil
		})
	}
	if slices.Contains(claim.Spec.AccessModes, corev1.ReadWriteMany) {
		return nodes, nil
	}
	var pods corev1.PodList
	if err := reader.List(ctx, &pods, client.InNamespace(namespace), client.MatchingLabels{RuntimeCacheClaimLabel: string(claim.UID)}); err != nil {
		return nil, fmt.Errorf("read runtime cache placement: %w", err)
	}
	placed := make(map[string]bool)
	for _, pod := range pods.Items {
		if pod.Spec.NodeName != "" && pod.Status.Phase != corev1.PodSucceeded && pod.Status.Phase != corev1.PodFailed {
			placed[pod.Spec.NodeName] = true
		}
	}
	if len(placed) == 0 {
		return nodes, nil
	}
	// Required Pod affinity uses the hostname topology label, not an inferred attachment location.
	hostnames := make(map[string]bool)
	for _, node := range nodes {
		if hostname := node.Labels[corev1.LabelHostname]; placed[node.Name] && hostname != "" {
			hostnames[hostname] = true
		}
	}
	return slices.DeleteFunc(slices.Clone(nodes), func(node corev1.Node) bool {
		return !hostnames[node.Labels[corev1.LabelHostname]]
	}), nil
}

// runtimeCacheClaim reads the selected claim for workload placement without adopting its lifecycle.
func runtimeCacheClaim(ctx context.Context, reader client.Reader, namespace string, cache *inferencev1alpha1.RuntimeCacheBinding) (*corev1.PersistentVolumeClaim, error) {
	if cache == nil {
		return nil, nil
	}
	claim := new(corev1.PersistentVolumeClaim)
	if err := reader.Get(ctx, client.ObjectKey{Namespace: namespace, Name: cache.ClaimName}, claim); err != nil {
		return nil, fmt.Errorf("read runtime cache claim: %w", err)
	}
	return claim, nil
}

// CacheObserverEnv gives preparation and serving processes the same filesystem observer identity.
func CacheObserverEnv(cache inferencev1alpha1.RuntimeCacheBinding, runtimePort int32) []corev1.EnvVar {
	return []corev1.EnvVar{
		{Name: "FORETOKEN_CACHE_MOUNT_PATH", Value: cache.MountPath},
		{Name: CacheClaimEnv, Value: cache.ClaimName},
		{Name: "FORETOKEN_CACHE_OBSERVATION_PORT", Value: strconv.Itoa(int(CacheObservationPort(runtimePort)))},
	}
}

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
