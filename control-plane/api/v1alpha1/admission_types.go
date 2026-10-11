// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines request admission intent shared by frontend defaults and model overrides.

package v1alpha1

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
)

// AdmissionConfig bounds waiting generation units for one frontend service and public model.
// +kubebuilder:pruning:PreserveUnknownFields
type AdmissionConfig struct {
	// MaxWaitingRequests is shared across frontend replicas; omission leaves waiting unrestricted.
	// +optional
	// +kubebuilder:validation:Format=int64
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=4294967295
	MaxWaitingRequests *uint32 `json:"maxWaitingRequests,omitempty"`

	// QueueTimeout bounds preparation and waiting until backend acceptance within the request deadline.
	// +optional
	QueueTimeout Duration `json:"queueTimeout,omitempty"`

	// UnrecognizedFields retains unsupported intent across API reads and controller patches.
	UnrecognizedFields map[string]json.RawMessage `json:"-"`
}

// UnmarshalJSON retains unsupported fields so controllers reject them before selecting applications.
func (config *AdmissionConfig) UnmarshalJSON(data []byte) error {
	type fields AdmissionConfig
	var decoded fields
	if err := json.Unmarshal(data, &decoded); err != nil {
		return err
	}
	var unrecognized map[string]json.RawMessage
	if err := json.Unmarshal(data, &unrecognized); err != nil {
		return err
	}
	delete(unrecognized, "maxWaitingRequests")
	delete(unrecognized, "queueTimeout")
	if len(unrecognized) > 0 {
		decoded.UnrecognizedFields = unrecognized
	}
	*config = AdmissionConfig(decoded)
	return nil
}

// MarshalJSON preserves unsupported intent rather than silently removing it during status or spec patches.
func (config AdmissionConfig) MarshalJSON() ([]byte, error) {
	type fields AdmissionConfig
	data, err := json.Marshal(fields(config))
	if err != nil || len(config.UnrecognizedFields) == 0 {
		return data, err
	}
	var encoded map[string]json.RawMessage
	if err := json.Unmarshal(data, &encoded); err != nil {
		return nil, err
	}
	for name, value := range config.UnrecognizedFields {
		encoded[name] = value
	}
	return json.Marshal(encoded)
}

// Validate rejects unsupported admission intent without exposing its values in controller errors.
func (config *AdmissionConfig) Validate() error {
	if config == nil || len(config.UnrecognizedFields) == 0 {
		return nil
	}
	names := make([]string, 0, len(config.UnrecognizedFields))
	for name := range config.UnrecognizedFields {
		names = append(names, name)
	}
	slices.Sort(names)
	return fmt.Errorf("unsupported admission fields: %s", strings.Join(names, ", "))
}

// CallerCapacity bounds each caller's generation units across frontend replicas.
type CallerCapacity struct {
	// +kubebuilder:validation:Format=int64
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=4294967295
	MaxWaitingRequests uint32 `json:"maxWaitingRequests"`

	// MaxConcurrentRequests includes dispatch reservations and accepted, unfinished work.
	// +kubebuilder:validation:Format=int64
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=4294967295
	MaxConcurrentRequests uint32 `json:"maxConcurrentRequests"`
}

// RoleRule resolves trusted caller roles into dispatch priority, capacity, and eligible Pools.
type RoleRule struct {
	// +kubebuilder:validation:MinLength=1
	Role string `json:"role"`

	// Priority gives higher values earlier dispatch.
	// +kubebuilder:validation:Minimum=-2147483648
	// +kubebuilder:validation:Maximum=2147483647
	Priority int32 `json:"priority"`

	PerCaller CallerCapacity `json:"perCaller"`

	// AllowedPools names this model's Pools; omission retains the normal candidate range.
	// +optional
	// +listType=set
	// +kubebuilder:validation:items:MinLength=1
	AllowedPools []string `json:"allowedPools,omitempty"`
}
