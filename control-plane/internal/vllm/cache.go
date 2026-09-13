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

// RuntimeCacheEnv returns vLLM model and compilation cache environment for one ModelGroup.
func RuntimeCacheEnv(cache *inferencev1alpha1.RuntimeCacheBinding) []corev1.EnvVar {
	env := make([]corev1.EnvVar, 0, 5)
	if cache != nil {
		env = append(env,
			corev1.EnvVar{Name: runtimeconfig.ModelRootEnv, Value: runtimeconfig.ModelDirectory(cache.MountPath)},
			corev1.EnvVar{Name: "HF_HOME", Value: runtimeconfig.ModelDirectory(cache.MountPath)},
			corev1.EnvVar{Name: "VLLM_CACHE_ROOT", Value: path.Join(cache.MountPath, "vllm")},
			corev1.EnvVar{Name: "TORCHINDUCTOR_CACHE_DIR", Value: path.Join(cache.MountPath, "torch")},
			corev1.EnvVar{Name: "TRITON_CACHE_DIR", Value: path.Join(cache.MountPath, "triton")},
		)
	}
	return env
}

// ModelSourceEnv extends the shared source environment with vLLM's provider selector.
func ModelSourceEnv(source *inferencev1alpha1.ModelSourceAccess, modelRoot string) []corev1.EnvVar {
	env := runtimeconfig.ModelSourceEnv(source, modelRoot)
	useModelScope := source != nil && source.Provider == inferencev1alpha1.ModelSourceProviderModelScope
	return append(env, corev1.EnvVar{Name: "VLLM_USE_MODELSCOPE", Value: strconv.FormatBool(useModelScope)})
}
