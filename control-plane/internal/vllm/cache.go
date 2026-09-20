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

// RuntimeCacheEnv returns vLLM model and compilation cache environment for one ModelGroup.
func RuntimeCacheEnv(cache *inferencev1alpha1.RuntimeCacheBinding) []corev1.EnvVar {
	env := make([]corev1.EnvVar, 0, 5)
	if cache != nil {
		env = append(env,
			corev1.EnvVar{Name: runtimeconfig.ModelRootEnv, Value: runtimeconfig.ModelDirectory(cache.MountPath)},
			corev1.EnvVar{Name: "HF_HOME", Value: runtimeconfig.ModelDirectory(cache.MountPath)},
			corev1.EnvVar{Name: "VLLM_CACHE_ROOT", Value: path.Join(cache.MountPath, "vllm")},
			corev1.EnvVar{Name: "TORCHINDUCTOR_CACHE_DIR", Value: path.Join(cache.MountPath, "torch")},
			// Triton atomically replaces loaded launchers; use Pod-local POSIX storage.
			corev1.EnvVar{Name: "TRITON_CACHE_DIR", Value: "/tmp/foretoken-runtime-cache/triton"},
		)
	}
	return env
}
