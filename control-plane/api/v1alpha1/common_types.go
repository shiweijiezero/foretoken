// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Defines shared validated types used by the Foretoken custom resources.

package v1alpha1

// ResourceQuantity is a non-negative Kubernetes resource quantity.
// +kubebuilder:validation:MinLength=1
// +kubebuilder:validation:MaxLength=64
// +kubebuilder:validation:XValidation:rule="isQuantity(self) && quantity(self).compareTo(quantity('0')) >= 0",message="must be a valid non-negative Kubernetes resource quantity"
type ResourceQuantity string

// Duration is a Go-style duration.
// +kubebuilder:validation:MinLength=2
// +kubebuilder:validation:Pattern="^([0-9]+(\\.[0-9]+)?(ns|us|µs|ms|s|m|h))+$"
type Duration string

// BackendArg is one backend command-line flag.
// +kubebuilder:validation:MinLength=2
// +kubebuilder:validation:MaxLength=4096
// +kubebuilder:validation:Pattern="^--"
type BackendArg string

// RevisionDigest identifies immutable compiled content by SHA-256 digest.
// +kubebuilder:validation:Pattern="^sha256:[a-f0-9]{64}$"
type RevisionDigest string

// ComputeResourceRequests defines the required CPU and memory for one Pod.
type ComputeResourceRequests struct {
	// CPU is the requested Kubernetes CPU quantity.
	CPU ResourceQuantity `json:"cpu"`

	// Memory is the requested Kubernetes memory quantity.
	Memory ResourceQuantity `json:"memory"`
}

// ComputeResourceLimits defines optional CPU and memory limits for one Pod.
type ComputeResourceLimits struct {
	// CPU is the maximum Kubernetes CPU quantity.
	// +optional
	CPU *ResourceQuantity `json:"cpu,omitempty"`

	// Memory is the maximum Kubernetes memory quantity.
	// +optional
	Memory *ResourceQuantity `json:"memory,omitempty"`
}

// FrontendResources defines resources for one frontend replica Pod.
// +kubebuilder:validation:XValidation:rule="!has(self.limits) || !has(self.limits.cpu) || quantity(self.requests.cpu).compareTo(quantity(self.limits.cpu)) <= 0",message="resources.requests.cpu must not exceed resources.limits.cpu"
// +kubebuilder:validation:XValidation:rule="!has(self.limits) || !has(self.limits.memory) || quantity(self.requests.memory).compareTo(quantity(self.limits.memory)) <= 0",message="resources.requests.memory must not exceed resources.limits.memory"
type FrontendResources struct {
	Requests ComputeResourceRequests `json:"requests"`

	// +optional
	Limits *ComputeResourceLimits `json:"limits,omitempty"`
}

// GPURequest defines the logical GPU request for one runtime member Pod.
type GPURequest struct {
	// Type is a platform-registered GPU type or auto.
	// +optional
	// +kubebuilder:default=auto
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern="^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$"
	Type string `json:"type,omitempty"`

	// Count is the number of GPUs requested by one runtime member Pod.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	Count int32 `json:"count,omitempty"`
}

// ModelResourceRequests defines resources requested by one runtime member Pod.
type ModelResourceRequests struct {
	ComputeResourceRequests `json:",inline"`

	GPU GPURequest `json:"gpu"`
}

// ModelResources defines resources for one runtime member Pod.
// +kubebuilder:validation:XValidation:rule="!has(self.limits) || !has(self.limits.cpu) || quantity(self.requests.cpu).compareTo(quantity(self.limits.cpu)) <= 0",message="resources.requests.cpu must not exceed resources.limits.cpu"
// +kubebuilder:validation:XValidation:rule="!has(self.limits) || !has(self.limits.memory) || quantity(self.requests.memory).compareTo(quantity(self.limits.memory)) <= 0",message="resources.requests.memory must not exceed resources.limits.memory"
type ModelResources struct {
	Requests ModelResourceRequests `json:"requests"`

	// +optional
	Limits *ComputeResourceLimits `json:"limits,omitempty"`
}
