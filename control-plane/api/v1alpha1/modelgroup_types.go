// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the controller-owned v1alpha1 ModelGroup custom-resource API.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// ModelGroupAccelerator defines the resolved Kubernetes accelerator placement.
type ModelGroupAccelerator struct {
	// DeviceResourceName is the Kubernetes extended resource requested by each member Pod.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	DeviceResourceName string `json:"deviceResourceName"`

	// RuntimeClassName selects the container runtime required by this accelerator profile.
	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	RuntimeClassName string `json:"runtimeClassName,omitempty"`

	// NodeSelector optionally constrains members to the resolved GPU profile.
	// +optional
	// +kubebuilder:validation:MaxProperties=16
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`
}

// ModelGroupArtifacts identifies immutable model and tokenizer inputs.
type ModelGroupArtifacts struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Model string `json:"model"`

	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	ModelRevision string `json:"modelRevision"`

	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Tokenizer string `json:"tokenizer"`

	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	TokenizerRevision string `json:"tokenizerRevision"`
}

// ModelGroupPDRuntimeConfig defines the resolved P/D transport runtime.
type ModelGroupPDRuntimeConfig struct {
	// ProfileName and ProfileRevision are opaque platform-owned identifiers.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	ProfileName string `json:"profileName"`

	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	ProfileRevision string `json:"profileRevision"`

	// +kubebuilder:validation:Enum=MooncakeConnector
	Connector string `json:"connector"`

	// +kubebuilder:validation:Enum=rdma
	Protocol string `json:"protocol"`

	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	BootstrapPort int32 `json:"bootstrapPort"`

	// +kubebuilder:validation:Minimum=1
	AbortRequestTimeoutSeconds int32 `json:"abortRequestTimeoutSeconds"`

	// RDMADeviceName selects the platform-verified HCA shared by both P/D roles.
	// +kubebuilder:validation:MinLength=1
	RDMADeviceName string `json:"rdmaDeviceName"`

	// RDMAResourceName is the platform-owned Kubernetes extended resource
	// whose device plugin injects the P/D transport devices.
	// +kubebuilder:validation:MinLength=1
	RDMAResourceName string `json:"rdmaResourceName"`

	// +kubebuilder:validation:Minimum=1
	RDMAResourceCount int32 `json:"rdmaResourceCount"`
}

// ECTransferRole is the fixed role assigned to a controller-owned EC runtime.
// +enum
// +kubebuilder:validation:Enum=producer;consumer
type ECTransferRole string

const (
	ECTransferRoleProducer ECTransferRole = "producer"
	ECTransferRoleConsumer ECTransferRole = "consumer"
)

// ModelGroupECRuntimeConfig is the immutable encoder/prefill transfer configuration.
// The controller resolves every value from a platform profile; users cannot supply
// connector options, module paths, or peer endpoints.
type ModelGroupECRuntimeConfig struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	ProfileName string `json:"profileName"`

	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	ProfileRevision string `json:"profileRevision"`

	// +kubebuilder:validation:Enum=ECExampleConnector
	Connector string `json:"connector"`

	Role ECTransferRole `json:"role"`

	// SharedStorageClaim is the platform-owned ReadWriteMany PVC used by the
	// the local vLLM build source ECExampleConnector.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=253
	SharedStorageClaim string `json:"sharedStorageClaim"`

	// SharedStoragePath is the fixed in-container connector path.
	// +kubebuilder:validation:Pattern=`^/[^[:space:]]*$`
	// +kubebuilder:validation:MaxLength=256
	SharedStoragePath string `json:"sharedStoragePath"`
}

// ModelGroupKVOffloadRuntime defines resolved local KV offload settings.
type ModelGroupKVOffloadRuntime struct {
	// +kubebuilder:validation:Minimum=1
	CPUBytes int64 `json:"cpuBytes"`

	// +optional
	Filesystem bool `json:"filesystem,omitempty"`
}

// ModelGroupMooncakeStoreRuntime defines the resolved external Store configuration.
type ModelGroupMooncakeStoreRuntime struct {
	// +kubebuilder:validation:MinLength=1
	ProfileName string `json:"profileName"`
	// +optional
	KVServiceUID string `json:"kvServiceUid,omitempty"`
	// RequesterBufferBytes is per-rank private Store memory included in the
	// container memory budget, not additional runtime overhead.
	// +optional
	RequesterBufferBytes int64 `json:"requesterBufferBytes,omitempty"`
	// +kubebuilder:validation:MinLength=1
	ProfileRevision string `json:"profileRevision"`
	// +kubebuilder:validation:MinLength=1
	ConfigMapName string `json:"configMapName"`
	// +kubebuilder:validation:MinLength=1
	ConfigMapKey string `json:"configMapKey"`
	// +kubebuilder:validation:MinLength=1
	PythonHashSeed string `json:"pythonHashSeed"`
}

// ModelGroupKVRuntimeConfig is the immutable resolved KV storage-mode configuration.
// +kubebuilder:validation:XValidation:rule="(has(self.offload) && !has(self.mooncakeStore)) || (!has(self.offload) && has(self.mooncakeStore))",message="kvRuntime must select exactly one KV storage mode"
type ModelGroupKVRuntimeConfig struct {
	// +optional
	Offload *ModelGroupKVOffloadRuntime `json:"offload,omitempty"`
	// +optional
	MooncakeStore *ModelGroupMooncakeStoreRuntime `json:"mooncakeStore,omitempty"`
}

// ModelGroupRuntime defines the resolved inference-engine runtime.
type ModelGroupRuntime struct {
	// +kubebuilder:validation:Enum=vllm
	Backend string `json:"backend"`

	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=2048
	Image string `json:"image"`

	// Port is the internal Foretoken model-server transport port.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port"`

	// Args contains inference-engine flags not represented by the typed Group specification.
	// +optional
	// +listType=atomic
	// +kubebuilder:validation:MaxItems=256
	Args []BackendArg `json:"args,omitempty"`

	// InternalGenerateRequestBodyLimitBytes is the group-local generate request
	// body limit resolved from the ModelService specification.
	// +kubebuilder:validation:Minimum=1048576
	// +kubebuilder:validation:Maximum=268435456
	InternalGenerateRequestBodyLimitBytes int64 `json:"internalGenerateRequestBodyLimitBytes"`
}

// ModelGroupSpec is an immutable execution specification compiled by the Pool controller.
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="ModelGroup spec is immutable"
// +kubebuilder:validation:XValidation:rule="self.memberCount == self.nodeCount",message="memberCount must equal nodeCount in v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.nodeCount * self.resources.requests.gpu.count == self.parallelism.pp * self.parallelism.tp * self.parallelism.pcp * self.parallelism.dp",message="accelerator capacity must equal the compiled worker rank count"
// +kubebuilder:validation:XValidation:rule="self.role == 'aggregate' ? !has(self.pdRuntime) && !has(self.ecRuntime) : self.role == 'encoder' ? !has(self.pdRuntime) && has(self.ecRuntime) && self.ecRuntime.role == 'producer' : self.role == 'prefill' ? has(self.pdRuntime) && (!has(self.ecRuntime) || self.ecRuntime.role == 'consumer') : self.role == 'decode' ? has(self.pdRuntime) && !has(self.ecRuntime) : false",message="aggregate, encoder, prefill, and decode ModelGroups require their fixed runtime configurations"
type ModelGroupSpec struct {
	ModelPoolRef LocalObjectReference `json:"modelPoolRef"`

	// Revision identifies one resolved Pool execution specification.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
	Revision string `json:"revision"`

	// Ordinal is unique within one Pool revision.
	// +kubebuilder:validation:Minimum=0
	Ordinal int32 `json:"ordinal"`

	Role ModelRole `json:"role"`

	Artifacts ModelGroupArtifacts `json:"artifacts"`
	Runtime   ModelGroupRuntime   `json:"runtime"`

	// PDRuntime is the controller-owned P/D transport configuration.
	// +optional
	PDRuntime *ModelGroupPDRuntimeConfig `json:"pdRuntime,omitempty"`

	// ECRuntime is the controller-owned resolved encoder/prefill transfer configuration.
	// +optional
	ECRuntime *ModelGroupECRuntimeConfig `json:"ecRuntime,omitempty"`

	// KVRuntime is the controller-owned resolved KV storage-mode configuration.
	// +optional
	KVRuntime *ModelGroupKVRuntimeConfig `json:"kvRuntime,omitempty"`

	Resources ModelResources `json:"resources"`
	Timeouts  ModelTimeouts  `json:"timeouts"`

	// NodeCount is the number of physical Kubernetes Nodes used by this Group.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=1
	NodeCount int32 `json:"nodeCount"`

	// MemberCount is the number of runtime member Pods in this Group.
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=1
	MemberCount int32 `json:"memberCount"`

	Parallelism CompiledParallelism `json:"parallelism"`

	// MaxInputTokens is the prompt admission limit advertised to routing clients.
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxInputTokens *int32 `json:"maxInputTokens,omitempty"`

	// Features is the immutable capabilities advertised to routing clients.
	Features ModelFeatures `json:"features"`

	Accelerator ModelGroupAccelerator `json:"accelerator"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern="^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$"
	Network string `json:"network,omitempty"`
}

// ModelGroupPhase summarizes the Group lifecycle.
// +enum
// +kubebuilder:validation:Enum=Pending;Provisioning;Ready;Draining;Failed;Terminating
type ModelGroupPhase string

const (
	ModelGroupPhasePending      ModelGroupPhase = "Pending"
	ModelGroupPhaseProvisioning ModelGroupPhase = "Provisioning"
	ModelGroupPhaseReady        ModelGroupPhase = "Ready"
	ModelGroupPhaseDraining     ModelGroupPhase = "Draining"
	ModelGroupPhaseFailed       ModelGroupPhase = "Failed"
	ModelGroupPhaseTerminating  ModelGroupPhase = "Terminating"
)

// ModelGroupStatus defines the observed state of a ModelGroup.
type ModelGroupStatus struct {
	// +optional
	Phase ModelGroupPhase `json:"phase,omitempty"`

	// DrainStartedAt fixes the bounded deletion deadline across controller retries.
	// +optional
	DrainStartedAt *metav1.Time `json:"drainStartedAt,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=0
	ReadyMembers int32 `json:"readyMembers,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=0
	TotalMembers int32 `json:"totalMembers,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	// +kubebuilder:validation:MaxItems=8
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Role",type=string,JSONPath=".spec.role"
// +kubebuilder:printcolumn:name="Revision",type=string,JSONPath=".spec.revision"
// +kubebuilder:printcolumn:name="Members",type=integer,JSONPath=".spec.memberCount"
// +kubebuilder:printcolumn:name="Ready",type=integer,JSONPath=".status.readyMembers"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=".status.phase"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// ModelGroup is one complete scale, readiness, failure, and lifecycle unit.
type ModelGroup struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              ModelGroupSpec   `json:"spec"`
	Status            ModelGroupStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ModelGroupList contains ModelGroup resources.
type ModelGroupList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ModelGroup `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &ModelGroup{}, &ModelGroupList{})
		return nil
	})
}
