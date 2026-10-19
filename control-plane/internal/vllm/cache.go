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

// runtimeCacheDirectory preserves the cache layout of the selected executable.
func runtimeCacheDirectory(root, namespace, deploymentUID string, preparationVersion int32) string {
	if preparationVersion < runtimeconfig.SharedModelCachePreparationVersion {
		return path.Join(root, "runtime", namespace)
	}
	return runtimeconfig.DeploymentCacheDirectory(root, deploymentUID)
}

// TritonCacheDirectory resolves the PVC cache template for an immutable Group runtime.
// Deployment and node identity preserve instance caches across Pod replacement without sharing writers.
func TritonCacheDirectory(cache *inferencev1alpha1.RuntimeCacheBinding, preparationVersion int32) string {
	if cache == nil {
		return ""
	}
	return path.Join(runtimeCacheDirectory(cache.MountPath, "$("+cacheNamespaceEnv+")", "$("+runtimeconfig.DeploymentUIDEnv+")", preparationVersion), "triton", "$("+cacheNodeNameEnv+")")
}

// RuntimeCacheEnv projects cache paths supported by the selected model-server application.
// Native and v1 applications retain their earlier storage outside managed shared-cache collection.
func RuntimeCacheEnv(cache *inferencev1alpha1.RuntimeCacheBinding, namespace, deploymentUID, tritonCacheDirectory string, preparationVersion int32) []corev1.EnvVar {
	root := "/tmp/foretoken-runtime-cache"
	if cache != nil {
		root = cache.MountPath
	}
	// Runtime identities need not have a passwd entry or a writable image home.
	user := strconv.FormatInt(runtimeconfig.CacheStorageAccess(cache).Identity.UID, 10)
	runtimeRoot := runtimeCacheDirectory(root, namespace, deploymentUID, preparationVersion)
	modelRoot := runtimeconfig.ModelDirectory(root)
	home := modelRoot
	shared := preparationVersion >= runtimeconfig.SharedModelCachePreparationVersion
	if shared {
		home = path.Join(runtimeRoot, "huggingface")
	}
	env := []corev1.EnvVar{
		{Name: cacheNamespaceEnv, Value: namespace},
		{Name: runtimeconfig.DeploymentUIDEnv, Value: deploymentUID},
		{Name: "HOME", Value: runtimeRoot},
		{Name: "USER", Value: user},
		{Name: "LOGNAME", Value: user},
		{Name: "XDG_CACHE_HOME", Value: path.Join(runtimeRoot, ".cache")},
		{Name: runtimeconfig.ModelRootEnv, Value: modelRoot},
		{Name: "HF_HOME", Value: home},
		{Name: "VLLM_CACHE_ROOT", Value: path.Join(runtimeRoot, "vllm")},
		{Name: "TORCHINDUCTOR_CACHE_DIR", Value: path.Join(runtimeRoot, "torch")},
	}
	if shared {
		modelCache := runtimeconfig.ModelCacheDirectory(root)
		env = append(env,
			corev1.EnvVar{Name: "XDG_CONFIG_HOME", Value: path.Join(runtimeRoot, ".config")},
			corev1.EnvVar{Name: runtimeconfig.ModelCacheRootEnv, Value: modelCache},
			corev1.EnvVar{Name: runtimeconfig.PreparationRootEnv, Value: path.Join(runtimeRoot, "prepared")},
			corev1.EnvVar{Name: "HF_HUB_CACHE", Value: path.Join(home, "hub")},
			corev1.EnvVar{Name: "VLLM_CONFIG_ROOT", Value: path.Join(runtimeRoot, ".config", "vllm")},
		)
	}
	if cache != nil {
		env = append(env, corev1.EnvVar{Name: runtimeconfig.RuntimeCacheBindingEnv, Value: runtimeconfig.RuntimeCacheIdentity(namespace, cache.ClaimName, preparationVersion)})
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
