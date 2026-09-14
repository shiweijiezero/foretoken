// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects Hugging Face access settings into serving workload environment variables.

package runtimeconfig

import (
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

// HuggingFaceEnv returns the optional endpoint and credential used only by Hugging Face clients.
func HuggingFaceEnv(access *inferencev1alpha1.HuggingFaceAccess) []corev1.EnvVar {
	if access == nil {
		return nil
	}
	env := make([]corev1.EnvVar, 0, 2)
	if access.Endpoint != "" {
		env = append(env, corev1.EnvVar{Name: "HF_ENDPOINT", Value: access.Endpoint})
	}
	if access.TokenSecretName != "" {
		env = append(env, corev1.EnvVar{
			Name: "HF_TOKEN",
			ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: access.TokenSecretName},
				Key:                  access.TokenSecretKey,
			}},
		})
	}
	return env
}
