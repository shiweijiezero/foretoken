// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Validates and projects the platform admission ledger connection without resolving credentials.

package runtimeconfig

import (
	"errors"
	"net/url"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

const AdmissionStoreURLEnv = "FORETOKEN_ADMISSION_STORE_URL"

// ValidateAdmissionStore checks the startup connection while keeping credentials out of errors.
func ValidateAdmissionStore(connection *inferencev1alpha1.AdmissionStoreConnection) error {
	if connection == nil || (connection.URL == "") == (connection.URLSecretRef == nil) {
		return errors.New("admission store requires exactly one URL or namespace-local Secret reference")
	}
	if secret := connection.URLSecretRef; secret != nil {
		if secret.Name == "" || secret.Key == "" || secret.Optional != nil && *secret.Optional {
			return errors.New("admission store URL Secret requires a name, key, and non-optional reference")
		}
		return nil
	}
	parsed, err := url.Parse(connection.URL)
	if err != nil || (parsed.Scheme != "redis" && parsed.Scheme != "rediss") || parsed.Hostname() == "" {
		return errors.New("admission store URL must be a Redis-compatible endpoint")
	}
	if parsed.User != nil {
		return errors.New("admission store credentials require a URL Secret reference")
	}
	return nil
}

// AdmissionStoreEnv gives serving Pods the resolved URL or Secret reference; kubelet owns credential reads.
func AdmissionStoreEnv(connection *inferencev1alpha1.AdmissionStoreConnection) []corev1.EnvVar {
	if connection == nil {
		return nil
	}
	env := corev1.EnvVar{Name: AdmissionStoreURLEnv, Value: connection.URL}
	if connection.URLSecretRef != nil {
		env.ValueFrom = &corev1.EnvVarSource{SecretKeyRef: connection.URLSecretRef.DeepCopy()}
	}
	return []corev1.EnvVar{env}
}
