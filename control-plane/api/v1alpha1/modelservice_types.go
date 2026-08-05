// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Defines the v1alpha1 ModelService custom-resource API.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// ModelTimeouts defines ModelService lifecycle budgets.
type ModelTimeouts struct {
	Startup Duration `json:"startup"`
	Drain   Duration `json:"drain"`
}

// ExpertParallelism defines expert-parallel execution for one ModelGroup.
type ExpertParallelism struct {
	// +kubebuilder:validation:Minimum=1
	Size int32 `json:"size"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	Backend string `json:"backend,omitempty"`

	// +optional
	// +kubebuilder:default=false
	EPLB bool `json:"eplb,omitempty"`
}

// Parallelism defines the execution topology of one ModelGroup.
// +kubebuilder:validation:XValidation:rule="!has(self.dp) || !has(self.ep)",message="parallelism.dp and parallelism.ep are mutually exclusive"
// +kubebuilder:validation:XValidation:rule="!has(self.ep) || self.ep.size % (self.tp * self.pcp) == 0",message="parallelism.ep.size must be divisible by parallelism.tp * parallelism.pcp"
// +kubebuilder:validation:XValidation:rule="self.pcp == 1 || !has(self.dp) || self.dp == 1",message="parallelism.pcp greater than 1 is incompatible with parallelism.dp greater than 1"
// +kubebuilder:validation:XValidation:rule="self.pcp == 1 || !has(self.ep) || self.ep.size == self.tp * self.pcp",message="parallelism.ep.size must equal parallelism.tp * parallelism.pcp when parallelism.pcp is greater than 1"
// +kubebuilder:validation:XValidation:rule="self.pcp == 1 ? self.tp % self.dcp == 0 : self.dcp == 1 || self.dcp == self.pcp || self.dcp == self.tp * self.pcp",message="parallelism.dcp is incompatible with parallelism.tp and parallelism.pcp"
type Parallelism struct {
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	TP int32 `json:"tp,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	PP int32 `json:"pp,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=1
	DP *int32 `json:"dp,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	PCP int32 `json:"pcp,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	DCP int32 `json:"dcp,omitempty"`

	// +optional
	EP *ExpertParallelism `json:"ep,omitempty"`
}

// ModelPoolTemplate defines one user-owned execution Pool: a homogeneous set
// of ModelGroups sharing the same role, network, resources, and parallelism.
// The controller instantiates it as a ModelPool owned by the ModelService.
// +kubebuilder:validation:XValidation:rule="(has(self.nodes) ? self.nodes : 1) * self.resources.requests.gpu.count == (has(self.parallelism.ep) ? self.parallelism.pp * self.parallelism.ep.size : self.parallelism.pp * self.parallelism.tp * self.parallelism.pcp * (has(self.parallelism.dp) ? self.parallelism.dp : 1))",message="nodes * resources.requests.gpu.count must equal the compiled worker rank count"
type ModelPoolTemplate struct {
	// Name is the stable identity of this Pool within one ModelService.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
	Name string `json:"name"`

	// +optional
	// +kubebuilder:default=aggregate
	Role ModelRole `json:"role,omitempty"`

	// Replicas is the number of complete ModelGroups in this Pool.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	Replicas *int32 `json:"replicas,omitempty"`

	// Nodes is the number of physical Kubernetes Nodes used by each ModelGroup.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	Nodes *int32 `json:"nodes,omitempty"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern="^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$"
	Network string `json:"network,omitempty"`

	Resources   ModelResources `json:"resources"`
	Parallelism Parallelism    `json:"parallelism"`
}

// ModelServiceSpec defines the desired state of a model service.
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || !(has(self.replicas) || has(self.nodes) || has(self.resources) || has(self.parallelism))",message="spec.modelPools is mutually exclusive with top-level replicas, nodes, resources, and parallelism"
// +kubebuilder:validation:XValidation:rule="has(self.modelPools) || (has(self.resources) && has(self.parallelism))",message="top-level resources and parallelism are required when spec.modelPools is omitted"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || self.modelPools.all(pool, pool.name != 'default')",message="modelPools name default is reserved for the Quick Start shorthand"
// +kubebuilder:validation:XValidation:rule="has(self.modelPools) || (has(self.resources) && has(self.parallelism) && (has(self.nodes) ? self.nodes : 1) * self.resources.requests.gpu.count == (has(self.parallelism.ep) ? self.parallelism.pp * self.parallelism.ep.size : self.parallelism.pp * self.parallelism.tp * self.parallelism.pcp * (has(self.parallelism.dp) ? self.parallelism.dp : 1)))",message="nodes * resources.requests.gpu.count must equal the compiled worker rank count"
type ModelServiceSpec struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Model string `json:"model"`

	// +kubebuilder:validation:Enum=vllm
	Backend string `json:"backend"`

	// Replicas is the number of complete ModelGroups in the default Pool; the compiler defaults it to 1.
	// +optional
	// +kubebuilder:validation:Minimum=0
	Replicas *int32 `json:"replicas,omitempty"`

	// Nodes is the number of physical Kubernetes Nodes used by each ModelGroup; the compiler defaults it to 1.
	// +optional
	// +kubebuilder:validation:Minimum=1
	Nodes *int32 `json:"nodes,omitempty"`

	// +optional
	Resources *ModelResources `json:"resources,omitempty"`

	Timeouts ModelTimeouts `json:"timeouts"`

	// +optional
	Parallelism *Parallelism `json:"parallelism,omitempty"`

	// ModelPools defines up to 32 advanced execution Pools instead of the top-level shorthand.
	// +optional
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=32
	ModelPools []ModelPoolTemplate `json:"modelPools,omitempty"`

	// ExtraArgs are backend CLI flags shared by every compiled Pool.
	// +optional
	// +listType=atomic
	// +kubebuilder:validation:MaxItems=256
	ExtraArgs []BackendArg `json:"extraArgs,omitempty"`
}

// ModelServiceStatus defines the observed state of a model service.
type ModelServiceStatus struct {
	// +optional
	// +kubebuilder:validation:Minimum=0
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	// +kubebuilder:validation:MaxItems=8
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Backend",type=string,JSONPath=".spec.backend"
// +kubebuilder:printcolumn:name="Replicas",type=integer,JSONPath=".spec.replicas"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// ModelService is the user-owned model serving API.
type ModelService struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              ModelServiceSpec   `json:"spec"`
	Status            ModelServiceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ModelServiceList contains ModelService resources.
type ModelServiceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ModelService `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &ModelService{}, &ModelServiceList{})
		return nil
	})
}
