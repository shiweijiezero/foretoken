// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects platform Hugging Face access settings into workload environments.

package runtimeconfig

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

// HuggingFaceAccessProfile configures an optional endpoint and credential for Hugging Face models.
type HuggingFaceAccessProfile struct {
	Endpoint        string
	TokenSecretName string
	TokenSecretKey  string
}

// Access returns the immutable settings copied only into Hugging Face workloads.
func (profile HuggingFaceAccessProfile) Access() *inferencev1alpha1.HuggingFaceAccess {
	if profile.Endpoint == "" && profile.TokenSecretName == "" {
		return nil
	}
	return &inferencev1alpha1.HuggingFaceAccess{Endpoint: profile.Endpoint, TokenSecretName: profile.TokenSecretName, TokenSecretKey: profile.TokenSecretKey}
}

// Validate rejects an incomplete credential reference.
func (profile HuggingFaceAccessProfile) Validate() error {
	if (profile.TokenSecretName == "") != (profile.TokenSecretKey == "") {
		return fmt.Errorf("Hugging Face Secret name and key must be configured together")
	}
	return nil
}

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
