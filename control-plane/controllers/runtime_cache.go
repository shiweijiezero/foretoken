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
	runtimeCacheVolumeName         = "runtime-cache"
	runtimeCacheClaimEnv           = "FORETOKEN_RUNTIME_CACHE_CLAIM"
	runtimeCacheFSGroup      int64 = 1000
	directoryOwnerAnnotation       = "inference.foretoken.io/directory-owner"
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

// runtimeCachePodSecurityContext selects PVC group access or the discovered host directory identity.
func runtimeCachePodSecurityContext(cache *inferencev1alpha1.RuntimeCacheBinding) *corev1.PodSecurityContext {
	context := &corev1.PodSecurityContext{SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}}
	if cache != nil {
		group := runtimeCacheFSGroup
		policy := corev1.FSGroupChangeOnRootMismatch
		context.FSGroup = &group
		context.FSGroupChangePolicy = &policy
		if owner := cache.DirectoryOwner; owner != nil {
			context.RunAsUser = &owner.UID
			context.RunAsGroup = &owner.GID
			context.FSGroup = &owner.GID
		}
	}
	return context
}

// runtimeCacheInitContainers prepares writable storage and migrates legacy root-owned local cache entries.
func runtimeCacheInitContainers(image string, cache *inferencev1alpha1.RuntimeCacheBinding) []corev1.Container {
	if cache == nil {
		return nil
	}
	command := []string{"sh", "-ec", `mkdir -p "$1"; chown 1000:1000 "$1"; chmod 2775 "$1"`, "prepare", cache.MountPath}
	if owner := cache.DirectoryOwner; owner != nil {
		// Restore the mount root to the recorded workload identity, then migrate root-owned
		// contents without following symlinks or changing shared hardlink inodes.
		command = []string{"sh", "-ec", `chown "$2:$3" "$1"; find "$1" -xdev -user 0 \( -type d -o -links 1 \) -exec chown -h "$2:$3" {} +; chmod u+rwx "$1"`, "prepare", cache.MountPath, strconv.FormatInt(owner.UID, 10), strconv.FormatInt(owner.GID, 10)}
	}
	root := int64(0)
	return []corev1.Container{{
		Name:    "runtime-cache-permissions",
		Image:   image,
		Command: command,
		Env:     []corev1.EnvVar{{Name: "NVIDIA_VISIBLE_DEVICES", Value: "void"}},
		SecurityContext: &corev1.SecurityContext{
			RunAsUser:  &root,
			RunAsGroup: &root,
			Capabilities: &corev1.Capabilities{
				Add:  []corev1.Capability{"CHOWN", "FOWNER", "DAC_OVERRIDE"},
				Drop: []corev1.Capability{"ALL"},
			},
		},
		VolumeMounts: []corev1.VolumeMount{{Name: runtimeCacheVolumeName, MountPath: cache.MountPath}},
	}}
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
	binding := &inferencev1alpha1.RuntimeCacheBinding{ClaimName: cache.Status.ClaimName, MountPath: profile.MountPath}
	if value := cache.Annotations[directoryOwnerAnnotation]; cache.Spec.Directory != "" && value != "" {
		uidText, gidText, found := strings.Cut(value, ":")
		uid, uidErr := strconv.ParseUint(uidText, 10, 32)
		gid, gidErr := strconv.ParseUint(gidText, 10, 32)
		if !found || uidErr != nil || gidErr != nil || uid == 0 || uid == 1<<32-1 || gid == 1<<32-1 {
			return nil, false, fmt.Errorf("RuntimeCache %q has invalid directory owner %q", cache.Name, value)
		}
		binding.DirectoryOwner = &inferencev1alpha1.RuntimeCacheDirectoryOwner{UID: int64(uid), GID: int64(gid)}
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
