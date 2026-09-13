// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects the platform model-source contract into serving workload environment variables.

package runtimeconfig

import (
	"path"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

const ModelSourceProviderEnv = "FORETOKEN_MODEL_SOURCE_PROVIDER"

// ModelSourceEnv returns provider-specific environment for frontend and inference-engine workloads.
func ModelSourceEnv(source *inferencev1alpha1.ModelSourceAccess, modelRoot string) []corev1.EnvVar {
	if source == nil {
		return nil
	}
	provider := source.Provider
	if provider == "" {
		provider = inferencev1alpha1.ModelSourceProviderHuggingFace
	}
	env := []corev1.EnvVar{{Name: ModelSourceProviderEnv, Value: string(provider)}}
	switch provider {
	case inferencev1alpha1.ModelSourceProviderHuggingFace:
		if source.Endpoint != "" {
			env = append(env, corev1.EnvVar{Name: "HF_ENDPOINT", Value: source.Endpoint})
		}
		if source.TokenSecretName != "" {
			env = append(env, secretEnv("HF_TOKEN", source))
		}
	case inferencev1alpha1.ModelSourceProviderModelScope:
		env = append(env, corev1.EnvVar{Name: "MODELSCOPE_CACHE", Value: path.Join(modelRoot, "modelscope")})
		if source.TokenSecretName != "" {
			env = append(env, secretEnv("MODELSCOPE_API_TOKEN", source))
		}
	}
	return env
}

func secretEnv(name string, source *inferencev1alpha1.ModelSourceAccess) corev1.EnvVar {
	return corev1.EnvVar{
		Name: name,
		ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: source.TokenSecretName},
			Key:                  source.TokenSecretKey,
		}},
	}
}
