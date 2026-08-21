// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the controller-owned v1alpha1 ModelPool custom-resource API.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// LocalObjectReference identifies an object in the same namespace by stable UID.
type LocalObjectReference struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`

	// +kubebuilder:validation:MinLength=1
	UID string `json:"uid"`
}

// ModelRole identifies a ModelPool's role in the serving path.
// +enum
// +kubebuilder:validation:Enum=aggregate;encoder;prefill;decode
type ModelRole string

const (
	ModelRoleAggregate ModelRole = "aggregate"
	ModelRoleEncoder   ModelRole = "encoder"
	ModelRolePrefill   ModelRole = "prefill"
	ModelRoleDecode    ModelRole = "decode"
)

// ManagedMooncakeStoreBinding pins a Ready Foretoken-owned KVService against ABA.
type ManagedMooncakeStoreBinding struct {
	Name            string `json:"name"`
	UID             string `json:"uid"`
	BindingRevision string `json:"bindingRevision"`
	ConfigMapName   string `json:"configMapName"`
	ConfigMapKey    string `json:"configMapKey"`
	// RequesterBufferBytes is per-rank private Store memory included in the
	// ModelGroup container memory budget; no hidden overhead is added.
	RequesterBufferBytes int64 `json:"requesterBufferBytes"`
}

// NormalizedMooncakeStore is either an external profile or a pinned managed binding.
// +kubebuilder:validation:XValidation:rule="has(self.profile) != has(self.managedBinding)",message="exactly one Store source is required"
type NormalizedMooncakeStore struct {
	Profile        string                       `json:"profile,omitempty"`
	ManagedBinding *ManagedMooncakeStoreBinding `json:"managedBinding,omitempty"`
}

// NormalizedKVCache is the controller-owned cache configuration attached to ModelPool.
// +kubebuilder:validation:XValidation:rule="(has(self.offload) && !has(self.mooncakeStore)) || (!has(self.offload) && has(self.mooncakeStore))",message="kvCache must select exactly one storage mode"
type NormalizedKVCache struct {
	Offload       *KVOffload               `json:"offload,omitempty"`
	MooncakeStore *NormalizedMooncakeStore `json:"mooncakeStore,omitempty"`
}

// NormalizedPoolTemplate is the normalized configuration produced from ModelService intent.
// Platform runtime and accelerator resolution may further constrain it before Groups are created.
// +kubebuilder:validation:XValidation:rule="self.memberCount == self.nodeCount",message="memberCount must equal nodeCount in v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.nodeCount * self.resources.requests.gpu.count == self.parallelism.pp * self.parallelism.tp * self.parallelism.pcp * self.parallelism.dp",message="accelerator capacity must equal the compiled worker rank count"
type NormalizedPoolTemplate struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Model string `json:"model"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	ModelRevision string `json:"modelRevision,omitempty"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Tokenizer string `json:"tokenizer,omitempty"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	TokenizerRevision string `json:"tokenizerRevision,omitempty"`

	// +kubebuilder:validation:Enum=vllm
	Backend string `json:"backend"`

	Role ModelRole `json:"role"`

	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=1
	NodeCount int32 `json:"nodeCount"`

	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=1
	MemberCount int32 `json:"memberCount"`

	Resources ModelResources `json:"resources"`

	Parallelism CompiledParallelism `json:"parallelism"`

	// MaxInputTokens is the immutable prompt admission limit for this Pool.
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxInputTokens *int32 `json:"maxInputTokens,omitempty"`

	// InternalGenerateRequestBodyLimitBytes is the resolved group-local generate
	// request body limit.
	// +kubebuilder:validation:Minimum=1048576
	// +kubebuilder:validation:Maximum=268435456
	InternalGenerateRequestBodyLimitBytes int64 `json:"internalGenerateRequestBodyLimitBytes"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern="^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$"
	Network string `json:"network,omitempty"`

	Timeouts ModelTimeouts `json:"timeouts"`

	// KVCache is immutable resolved cache-placement intent.
	// +optional
	KVCache *NormalizedKVCache `json:"kvCache,omitempty"`

	// Features is the normalized explicit capabilities for this Pool.
	Features ModelFeatures `json:"features"`

	// ECProfile is the controller-owned platform EC profile selected for encoder and prefill Pools.
	// +optional
	ECProfile string `json:"ecProfile,omitempty"`

	// ExtraArgs are inference-engine flags that the concrete adapter must validate before Group creation.
	// +optional
	// +listType=atomic
	// +kubebuilder:validation:MaxItems=256
	ExtraArgs []BackendArg `json:"extraArgs,omitempty"`
}

// ModelPoolSpec is the controller-owned desired state compiled from ModelService.
type ModelPoolSpec struct {
	ModelServiceRef LocalObjectReference `json:"modelServiceRef"`

	// PoolName is the stable identity of the source ModelService modelPools entry.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
	PoolName string `json:"poolName"`

	// DesiredGroups is the number of complete ModelGroups requested for this Pool.
	// +kubebuilder:validation:Minimum=0
	DesiredGroups int32 `json:"desiredGroups"`

	Template NormalizedPoolTemplate `json:"template"`
}

// ModelPoolStatus defines the observed state of a ModelPool.
type ModelPoolStatus struct {
	// +optional
	// +kubebuilder:validation:Minimum=0
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// PreparedRevision is the immutable Group revision whose requested capacity is ready for a service-level commit.
	// +optional
	// +kubebuilder:validation:MaxLength=63
	PreparedRevision string `json:"preparedRevision,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	// +kubebuilder:validation:MaxItems=8
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Role",type=string,JSONPath=".spec.template.role"
// +kubebuilder:printcolumn:name="Desired",type=integer,JSONPath=".spec.desiredGroups"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// ModelPool is a read-only execution fleet materialized from ModelService intent.
type ModelPool struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              ModelPoolSpec   `json:"spec"`
	Status            ModelPoolStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ModelPoolList contains ModelPool resources.
type ModelPoolList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ModelPool `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &ModelPool{}, &ModelPoolList{})
		return nil
	})
}
