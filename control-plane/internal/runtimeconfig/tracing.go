// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package runtimeconfig

import (
	"fmt"
	"math"
	"net/url"
	"strconv"

	corev1 "k8s.io/api/core/v1"
)

// Tracing configures optional OTLP export from controller-managed inference workloads.
// Credentials remain in namespace-local Secrets and are never read by the controller.
type Tracing struct {
	Endpoint          string
	SamplingRatio     float64
	HeadersSecretName string
	HeadersSecretKey  string
}

// Validate checks the platform's tracing configuration before manager startup.
func (config Tracing) Validate() error {
	if config.Endpoint == "" {
		if config.HeadersSecretName != "" {
			return fmt.Errorf("tracing headers require a tracing endpoint")
		}
		return nil
	}
	endpoint, err := url.Parse(config.Endpoint)
	if err != nil || endpoint.Host == "" || (endpoint.Scheme != "http" && endpoint.Scheme != "https") || endpoint.User != nil || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return fmt.Errorf("tracing endpoint must be an HTTP or HTTPS base URL without credentials, query, or fragment")
	}
	if math.IsNaN(config.SamplingRatio) || config.SamplingRatio < 0 || config.SamplingRatio > 1 {
		return fmt.Errorf("tracing sampling ratio must be between 0 and 1")
	}
	if config.HeadersSecretName != "" && config.HeadersSecretKey == "" {
		return fmt.Errorf("tracing headers Secret requires a key")
	}
	return nil
}

// Env returns the standard OTLP environment consumed by frontend and model-server Pods.
// An empty endpoint leaves export disabled; each process supplies its own service name.
func (config Tracing) Env() []corev1.EnvVar {
	if config.Endpoint == "" {
		return nil
	}
	env := []corev1.EnvVar{
		{Name: "OTEL_EXPORTER_OTLP_ENDPOINT", Value: config.Endpoint},
		{Name: "OTEL_EXPORTER_OTLP_PROTOCOL", Value: "http/protobuf"},
		{Name: "OTEL_TRACES_SAMPLER", Value: "parentbased_traceidratio"},
		{Name: "OTEL_TRACES_SAMPLER_ARG", Value: strconv.FormatFloat(config.SamplingRatio, 'g', -1, 64)},
		{Name: "FORETOKEN_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.namespace"}}},
		{Name: "FORETOKEN_POD_NAME", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.name"}}},
		{Name: "OTEL_RESOURCE_ATTRIBUTES", Value: "k8s.namespace.name=$(FORETOKEN_NAMESPACE),k8s.pod.name=$(FORETOKEN_POD_NAME)"},
	}
	if config.HeadersSecretName != "" {
		env = append(env, corev1.EnvVar{
			Name: "OTEL_EXPORTER_OTLP_HEADERS",
			ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: config.HeadersSecretName},
				Key:                  config.HeadersSecretKey,
			}},
		})
	}
	return env
}
