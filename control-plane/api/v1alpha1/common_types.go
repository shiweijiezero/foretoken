// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines shared validated types used by the Foretoken custom resources.

package v1alpha1

// ResourceQuantity is a non-negative Kubernetes resource quantity.
// +kubebuilder:validation:MinLength=1
// +kubebuilder:validation:MaxLength=64
// +kubebuilder:validation:XValidation:rule="isQuantity(self) && quantity(self).compareTo(quantity('0')) >= 0",message="must be a valid non-negative Kubernetes resource quantity"
type ResourceQuantity string

// Duration is a positive Go-style duration.
// +kubebuilder:validation:MinLength=2
// +kubebuilder:validation:Pattern="^([0-9]+(\\.[0-9]+)?(ns|us|µs|ms|s|m|h))+$"
// +kubebuilder:validation:XValidation:rule="duration(self) > duration('0s')",message="must be a positive duration"
type Duration string

// NonNegativeDuration is a Go-style duration that may be zero.
// +kubebuilder:validation:MinLength=2
// +kubebuilder:validation:Pattern="^([0-9]+(\\.[0-9]+)?(ns|us|µs|ms|s|m|h))+$"
// +kubebuilder:validation:XValidation:rule="duration(self) >= duration('0s')",message="must be a non-negative duration"
type NonNegativeDuration string

// BackendArg is one long-form CLI argument passed to the inference engine.
// The vLLM adapter accepts max-model-len, dtype, quantization, gpu-memory-utilization,
// max-num-seqs, max-num-batched-tokens, limit-mm-per-prompt, enforce-eager, and
// disable-log-stats. This schema rejects spaces, aliases, and positional arguments.
// +kubebuilder:validation:MinLength=3
// +kubebuilder:validation:MaxLength=4096
// +kubebuilder:validation:Pattern="^--[a-z][a-z0-9-]*(=[^[:space:]]+)?$"
type BackendArg string

// StructuredOutputFormat identifies a structured response format supported by a model.
// +enum
// +kubebuilder:validation:Enum=jsonObject;jsonSchema
type StructuredOutputFormat string

const (
	StructuredOutputFormatJSONObject StructuredOutputFormat = "jsonObject"
	StructuredOutputFormatJSONSchema StructuredOutputFormat = "jsonSchema"
)

// MultimodalModality identifies one non-text input modality supported by a model.
// +enum
// +kubebuilder:validation:Enum=image
type MultimodalModality string

const MultimodalModalityImage MultimodalModality = "image"

// ModelFeatures declares opt-in model capabilities. Chat and text are always
// available and therefore intentionally are not configurable here.
type ModelFeatures struct {
	// +optional
	Tools bool `json:"tools,omitempty"`

	// +optional
	Reasoning bool `json:"reasoning,omitempty"`

	// +optional
	// +listType=set
	// +kubebuilder:validation:MaxItems=2
	StructuredOutputs []StructuredOutputFormat `json:"structuredOutputs,omitempty"`

	// +optional
	// +listType=set
	// +kubebuilder:validation:MaxItems=1
	Multimodal []MultimodalModality `json:"multimodal,omitempty"`
}

// CompiledParallelism defines an explicit execution topology compiled from user intent.
// Unlike user input, DP is always present and may coexist with EP as its derived value.
// +kubebuilder:validation:XValidation:rule="!has(self.ep) || self.ep.size % (self.tp * self.pcp) == 0",message="parallelism.ep.size must be divisible by parallelism.tp * parallelism.pcp"
// +kubebuilder:validation:XValidation:rule="!has(self.ep) || self.dp == self.ep.size / (self.tp * self.pcp)",message="parallelism.dp must equal the value derived from parallelism.ep.size"
// +kubebuilder:validation:XValidation:rule="self.pcp == 1 || self.dp == 1",message="parallelism.pcp greater than 1 is incompatible with parallelism.dp greater than 1"
// +kubebuilder:validation:XValidation:rule="self.pcp == 1 ? self.tp % self.dcp == 0 : self.dcp == 1 || self.dcp == self.pcp || self.dcp == self.tp * self.pcp",message="parallelism.dcp is incompatible with parallelism.tp and parallelism.pcp"
type CompiledParallelism struct {
	// +kubebuilder:validation:Minimum=1
	TP int32 `json:"tp"`

	// +kubebuilder:validation:Minimum=1
	PP int32 `json:"pp"`

	// +kubebuilder:validation:Minimum=1
	DP int32 `json:"dp"`

	// +kubebuilder:validation:Minimum=1
	PCP int32 `json:"pcp"`

	// +kubebuilder:validation:Minimum=1
	DCP int32 `json:"dcp"`

	// +optional
	EP *ExpertParallelism `json:"ep,omitempty"`
}

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

// GPURequest defines the accelerator request for one runtime member Pod.
type GPURequest struct {
	// Count is the number of accelerator devices requested by one runtime member Pod.
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
