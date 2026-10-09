// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines request admission intent shared by frontend defaults and model overrides.

package v1alpha1

// AdmissionParameters sets per-model concurrency and queue limits in each frontend process.
type AdmissionParameters struct {
	// +kubebuilder:validation:Format=int64
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=4294967295
	MaxConcurrentRequests uint32 `json:"maxConcurrentRequests"`

	// +optional
	// +kubebuilder:validation:Format=int64
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:Maximum=4294967295
	MaxQueuedRequests uint32 `json:"maxQueuedRequests,omitempty"`

	// QueueTimeout limits queueing within the remaining request budget.
	// +optional
	QueueTimeout Duration `json:"queueTimeout,omitempty"`
}

// AdmissionConfig selects a request admission rule and its parameters.
// +kubebuilder:validation:XValidation:rule="self.algorithm != 'concurrency' || has(self.parameters)",message="concurrency admission requires parameters"
// +kubebuilder:validation:XValidation:rule="self.algorithm != 'allow_all' || !has(self.parameters)",message="allow_all admission accepts no parameters"
type AdmissionConfig struct {
	// +optional
	// +kubebuilder:default=allow_all
	// +kubebuilder:validation:MinLength=1
	Algorithm string `json:"algorithm,omitempty"`

	// +optional
	Parameters *AdmissionParameters `json:"parameters,omitempty"`
}
