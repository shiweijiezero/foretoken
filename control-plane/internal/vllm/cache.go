// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Maps the platform-neutral cache root to vLLM runtime directories.

package vllm

import (
	"path"
	"strconv"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	corev1 "k8s.io/api/core/v1"
)

const (
	cacheNamespaceEnv = "FORETOKEN_CACHE_NAMESPACE"
	cacheNodeNameEnv  = "FORETOKEN_CACHE_NODE_NAME"
)

// runtimeCacheDirectory isolates writable runtime state while model files remain shared.
func runtimeCacheDirectory(root, namespace string) string {
	return path.Join(root, "runtime", namespace)
}

// TritonCacheDirectory resolves the PVC cache template for an immutable Group runtime.
// Namespace and node identity preserve candidate caches across Pod replacement without sharing writers.
func TritonCacheDirectory(cache *inferencev1alpha1.RuntimeCacheBinding) string {
	if cache == nil {
		return ""
	}
	return path.Join(runtimeCacheDirectory(cache.MountPath, "$("+cacheNamespaceEnv+")"), "triton", "$("+cacheNodeNameEnv+")")
}

// RuntimeCacheEnv returns vLLM model and compilation cache environment for one ModelGroup.
func RuntimeCacheEnv(cache *inferencev1alpha1.RuntimeCacheBinding, namespace, tritonCacheDirectory string) []corev1.EnvVar {
	root := "/tmp/foretoken-runtime-cache"
	if cache != nil {
		root = cache.MountPath
	}
	// Runtime identities need not have a passwd entry or a writable image home.
	user := strconv.FormatInt(runtimeconfig.CacheStorageAccess(cache).Identity.UID, 10)
	runtimeRoot := runtimeCacheDirectory(root, namespace)
	env := []corev1.EnvVar{
		{Name: cacheNamespaceEnv, Value: namespace},
		{Name: "HOME", Value: runtimeRoot},
		{Name: "USER", Value: user},
		{Name: "LOGNAME", Value: user},
		{Name: "XDG_CACHE_HOME", Value: path.Join(runtimeRoot, ".cache")},
		{Name: runtimeconfig.ModelRootEnv, Value: runtimeconfig.ModelDirectory(root)},
		{Name: "HF_HOME", Value: runtimeconfig.ModelDirectory(root)},
		{Name: "VLLM_CACHE_ROOT", Value: path.Join(runtimeRoot, "vllm")},
		{Name: "TORCHINDUCTOR_CACHE_DIR", Value: path.Join(runtimeRoot, "torch")},
	}
	if cache != nil {
		env = append(env, corev1.EnvVar{Name: runtimeconfig.RuntimeCacheBindingEnv, Value: namespace + "/" + cache.ClaimName})
	}
	if tritonCacheDirectory != "" {
		// Kubernetes expands references only to earlier environment entries.
		env = append(env,
			corev1.EnvVar{Name: cacheNodeNameEnv, ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "spec.nodeName"}}},
			corev1.EnvVar{Name: "TRITON_CACHE_DIR", Value: tritonCacheDirectory},
		)
	} else {
		// Preparation and Groups without a resolved path keep compilation state Pod-local.
		env = append(env, corev1.EnvVar{Name: "TRITON_CACHE_DIR", Value: "/tmp/foretoken-runtime-cache/triton"})
	}
	return env
}
