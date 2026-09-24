// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Maps the platform-neutral cache root to vLLM runtime directories.

package vllm

import (
	"path"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	corev1 "k8s.io/api/core/v1"
)

const cacheNodeNameEnv = "FORETOKEN_CACHE_NODE_NAME"

// TritonCacheDirectory resolves the PVC cache template for an immutable Group runtime.
// Node identity isolates shared-filesystem clients without discarding cache on Pod replacement.
func TritonCacheDirectory(cache *inferencev1alpha1.RuntimeCacheBinding) string {
	if cache == nil {
		return ""
	}
	return path.Join(cache.MountPath, "triton", "$("+cacheNodeNameEnv+")")
}

// RuntimeCacheEnv returns vLLM model and compilation cache environment for one ModelGroup.
func RuntimeCacheEnv(cache *inferencev1alpha1.RuntimeCacheBinding, tritonCacheDirectory string) []corev1.EnvVar {
	env := make([]corev1.EnvVar, 0, 6)
	if cache != nil {
		env = append(env,
			corev1.EnvVar{Name: runtimeconfig.ModelRootEnv, Value: runtimeconfig.ModelDirectory(cache.MountPath)},
			corev1.EnvVar{Name: "HF_HOME", Value: runtimeconfig.ModelDirectory(cache.MountPath)},
			corev1.EnvVar{Name: "VLLM_CACHE_ROOT", Value: path.Join(cache.MountPath, "vllm")},
			corev1.EnvVar{Name: "TORCHINDUCTOR_CACHE_DIR", Value: path.Join(cache.MountPath, "torch")},
		)
	}
	if tritonCacheDirectory != "" {
		// Kubernetes expands references only to earlier environment entries.
		env = append(env,
			corev1.EnvVar{Name: cacheNodeNameEnv, ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "spec.nodeName"}}},
			corev1.EnvVar{Name: "TRITON_CACHE_DIR", Value: tritonCacheDirectory},
		)
	} else if cache != nil {
		// Groups without a resolved path retain their existing Pod template until
		// the Pool replaces them with the newly resolved runtime revision.
		env = append(env, corev1.EnvVar{Name: "TRITON_CACHE_DIR", Value: "/tmp/foretoken-runtime-cache/triton"})
	}
	return env
}
