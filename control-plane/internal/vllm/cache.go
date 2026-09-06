// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Maps the platform-neutral cache root to vLLM runtime directories.

package vllm

import (
	"path"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

// RuntimeCacheEnv returns vLLM model, source, and compilation cache environment for one ModelGroup.
func RuntimeCacheEnv(cache *inferencev1alpha1.RuntimeCache, source *inferencev1alpha1.RuntimeSourceAccess) []corev1.EnvVar {
	env := make([]corev1.EnvVar, 0, 6)
	if cache != nil {
		env = append(env,
			corev1.EnvVar{Name: "HF_HOME", Value: path.Join(cache.MountPath, "models")},
			corev1.EnvVar{Name: "VLLM_CACHE_ROOT", Value: path.Join(cache.MountPath, "vllm")},
			corev1.EnvVar{Name: "TORCHINDUCTOR_CACHE_DIR", Value: path.Join(cache.MountPath, "torch")},
			corev1.EnvVar{Name: "TRITON_CACHE_DIR", Value: path.Join(cache.MountPath, "triton")},
		)
	}
	if source != nil {
		if source.Endpoint != "" {
			env = append(env, corev1.EnvVar{Name: "HF_ENDPOINT", Value: source.Endpoint})
		}
		if source.TokenSecretName != "" {
			env = append(env, corev1.EnvVar{
				Name: "HF_TOKEN",
				ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
					LocalObjectReference: corev1.LocalObjectReference{Name: source.TokenSecretName},
					Key:                  source.TokenSecretKey,
				}},
			})
		}
	}
	return env
}
