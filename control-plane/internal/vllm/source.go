// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Maps platform source access to vLLM runtime environment.

package vllm

import (
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

// RuntimeSourceEnv returns model source environment for one vLLM ModelGroup.
func RuntimeSourceEnv(source *inferencev1alpha1.RuntimeSourceAccess) []corev1.EnvVar {
	env := make([]corev1.EnvVar, 0, 2)
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
