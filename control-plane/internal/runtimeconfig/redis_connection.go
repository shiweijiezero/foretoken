// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Validates and projects Redis-compatible connections without resolving credentials.

package runtimeconfig

import (
	"errors"
	"net/url"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

const AdmissionStoreURLEnv = "FORETOKEN_ADMISSION_STORE_URL"

// ValidateRedisConnection checks platform and recovered selections without exposing credentials in errors.
func ValidateRedisConnection(connection *inferencev1alpha1.RedisConnection) error {
	if connection == nil || (connection.URL == "") == (connection.URLSecretRef == nil) {
		return errors.New("Redis connection requires exactly one URL or namespace-local Secret reference")
	}
	if secret := connection.URLSecretRef; secret != nil {
		if secret.Name == "" || secret.Key == "" || secret.Optional != nil && *secret.Optional {
			return errors.New("Redis URL Secret requires a name, key, and non-optional reference")
		}
		return nil
	}
	parsed, err := url.Parse(connection.URL)
	if err != nil || (parsed.Scheme != "redis" && parsed.Scheme != "rediss") || parsed.Hostname() == "" {
		return errors.New("Redis URL must be a Redis-compatible endpoint")
	}
	if parsed.User != nil {
		return errors.New("Redis credentials require a URL Secret reference")
	}
	return nil
}

// RedisConnectionEnv gives a serving process its selected URL; kubelet owns credential reads.
func RedisConnectionEnv(connection *inferencev1alpha1.RedisConnection, name string) []corev1.EnvVar {
	if connection == nil {
		return nil
	}
	env := corev1.EnvVar{Name: name, Value: connection.URL}
	if connection.URLSecretRef != nil {
		env.ValueFrom = &corev1.EnvVarSource{SecretKeyRef: connection.URLSecretRef.DeepCopy()}
	}
	return []corev1.EnvVar{env}
}

// RedisConnectionFromEnv recovers a workload's selected endpoint without reading its Secret.
// A missing variable leaves applications with no selected connection unchanged.
func RedisConnectionFromEnv(environment []corev1.EnvVar, name string) (*inferencev1alpha1.RedisConnection, error) {
	var connection *inferencev1alpha1.RedisConnection
	for _, env := range environment {
		if env.Name != name {
			continue
		}
		connection = &inferencev1alpha1.RedisConnection{URL: env.Value}
		if env.ValueFrom != nil && env.ValueFrom.SecretKeyRef != nil {
			connection.URLSecretRef = env.ValueFrom.SecretKeyRef.DeepCopy()
		}
	}
	if connection == nil {
		return nil, nil
	}
	if err := ValidateRedisConnection(connection); err != nil {
		return nil, err
	}
	return connection, nil
}
