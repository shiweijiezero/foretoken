// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the user-owned Mooncake standalone Store API and its controller-owned Pools.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// ByteQuantity is a positive, exact integer number of bytes. It deliberately
// does not accept Kubernetes unit suffixes so cache capacities are unambiguous.
// +kubebuilder:validation:Pattern="^[1-9][0-9]*$"
// +kubebuilder:validation:MaxLength=19
type ByteQuantity string

// KVResources defines CPU and memory for a Mooncake component.
// +kubebuilder:validation:XValidation:rule="!has(self.limits) || !has(self.limits.cpu) || quantity(self.requests.cpu).compareTo(quantity(self.limits.cpu)) <= 0",message="resources.requests.cpu must not exceed resources.limits.cpu"
// +kubebuilder:validation:XValidation:rule="!has(self.limits) || !has(self.limits.memory) || quantity(self.requests.memory).compareTo(quantity(self.limits.memory)) <= 0",message="resources.requests.memory must not exceed resources.limits.memory"
type KVResources struct {
	Requests ComputeResourceRequests `json:"requests"`
	// +optional
	Limits *ComputeResourceLimits `json:"limits,omitempty"`
}

// RetentionPolicy controls controller behavior when an owned PVC is removed.
// Retain releases the PVC from its KVService owner; a later KVService does not
// adopt it, even when it has the same namespace and name.
// +enum
// +kubebuilder:validation:Enum=Delete;Retain
type RetentionPolicy string

const (
	RetentionPolicyDelete RetentionPolicy = "Delete"
	RetentionPolicyRetain RetentionPolicy = "Retain"
)

// SnapshotStorage configures the Master singleton snapshot volume. The provider's
// snapshot retention is not a Foretoken cache TTL or eviction policy.
type SnapshotStorage struct {
	// +optional
	StorageClassName string       `json:"storageClassName,omitempty"`
	Size             ByteQuantity `json:"size"`
	// +optional
	// +kubebuilder:default=Delete
	RetentionPolicy RetentionPolicy `json:"retentionPolicy,omitempty"`
}

// KVMasterSpec configures the KVService-level Mooncake Master singleton.
type KVMasterSpec struct {
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
	// +optional
	// +kubebuilder:default=50051
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	RPCPort int32 `json:"rpcPort,omitempty"`
	// +optional
	// +kubebuilder:default=8080
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	MetadataPort int32 `json:"metadataPort,omitempty"`
	// +optional
	// +kubebuilder:default=9003
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	MetricsPort int32       `json:"metricsPort,omitempty"`
	Resources   KVResources `json:"resources"`
	// SnapshotInterval controls the provider snapshot cadence for both persistent and ephemeral snapshot storage.
	// +optional
	// +kubebuilder:default="60s"
	// +kubebuilder:validation:Pattern="^([0-9]+(s|m|h))+$"
	SnapshotInterval Duration `json:"snapshotInterval,omitempty"`
	// SnapshotRetentionCount is the number of provider snapshots retained.
	// +optional
	// +kubebuilder:default=3
	// +kubebuilder:validation:Minimum=1
	SnapshotRetentionCount int32 `json:"snapshotRetentionCount,omitempty"`
	// +optional
	Snapshot *SnapshotStorage `json:"snapshot,omitempty"`
}

// KVDisk configures storage requested by each future client Group. Its size is a
// PVC request, not a claim about usable filesystem capacity.
type KVDisk struct {
	// +optional
	StorageClassName string       `json:"storageClassName,omitempty"`
	Size             ByteQuantity `json:"size"`
	// +optional
	// +kubebuilder:default=Delete
	RetentionPolicy RetentionPolicy `json:"retentionPolicy,omitempty"`
}

// KVClientTemplate is immutable normalized intent for homogeneous future clients.
// This standalone Store profile enables SSD offload, so disk is required. The
// user-provided gap between capacity and memory resources reserves runtime overhead;
// Foretoken deliberately does not guess a fixed overhead amount.
// +kubebuilder:validation:XValidation:rule="has(self.disk)",message="client.disk is required when standalone Store offload is enabled"
// +kubebuilder:validation:XValidation:rule="quantity(self.memoryCapacity).compareTo(quantity(self.resources.requests.memory)) < 0",message="client.memoryCapacity must be less than client.resources.requests.memory to reserve runtime overhead"
// +kubebuilder:validation:XValidation:rule="!has(self.resources.limits) || !has(self.resources.limits.memory) || quantity(self.memoryCapacity).compareTo(quantity(self.resources.limits.memory)) < 0",message="client.memoryCapacity must be less than client.resources.limits.memory to reserve runtime overhead"
type KVClientTemplate struct {
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
	// +kubebuilder:validation:Enum=rdma
	Protocol string `json:"protocol"`
	// +optional
	// +kubebuilder:default=50052
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port      int32       `json:"port,omitempty"`
	Resources KVResources `json:"resources"`
	// +kubebuilder:validation:MinLength=1
	RDMAResourceName string `json:"rdmaResourceName"`
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	RDMAResourceCount int32        `json:"rdmaResourceCount,omitempty"`
	MemoryCapacity    ByteQuantity `json:"memoryCapacity"`
	// +optional
	Disk *KVDisk `json:"disk,omitempty"`
}

// KVStoragePoolTemplate names one homogeneous future client capacity Pool.
type KVStoragePoolTemplate struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
	Name string `json:"name"`
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	Replicas int32            `json:"replicas,omitempty"`
	Client   KVClientTemplate `json:"client"`
	// +optional
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`
}

// KVTimeouts defines lifecycle budgets for future Store clients.
type KVTimeouts struct {
	Startup Duration `json:"startup"`
	Drain   Duration `json:"drain"`
}

// KVRequesterSpec configures ModelGroup Store requester configuration.
type KVRequesterSpec struct {
	// LocalBufferSize is an exact positive integer number of bytes.
	LocalBufferSize ByteQuantity `json:"localBufferSize"`
}

// KVServiceBinding is the immutable current requester configuration consumed by ModelPools.
type KVServiceBinding struct {
	Revision       string `json:"revision"`
	ConfigMapName  string `json:"configMapName"`
	ConfigMapKey   string `json:"configMapKey"`
	MasterEndpoint string `json:"masterEndpoint"`
	PythonHashSeed string `json:"pythonHashSeed"`
}

// KVServiceSpec declares a Foretoken-owned Mooncake standalone Store.
// +kubebuilder:validation:XValidation:rule="self.storagePools.all(pool, self.storagePools.exists(other, other.name == pool.name) ? self.storagePools.filter(other, other.name == pool.name).size() == 1 : true)",message="storagePools names must be unique"
type KVServiceSpec struct {
	// +kubebuilder:validation:Enum=mooncakeStandaloneStore
	Backend string       `json:"backend"`
	Master  KVMasterSpec `json:"master"`
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=32
	StoragePools []KVStoragePoolTemplate `json:"storagePools"`
	Timeouts     KVTimeouts              `json:"timeouts"`
	Requester    KVRequesterSpec         `json:"requester"`
}

// KVServiceStatus reports infrastructure and Pool materialization only; it does
// not claim provider registration, usable Store state, or available capacity.
type KVServiceStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	Phase KVServicePhase `json:"phase,omitempty"`
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
	// +optional
	Binding *KVServiceBinding `json:"binding,omitempty"`
}

// +enum
// +kubebuilder:validation:Enum=Pending;Progressing;Ready;Degraded;Terminating
type KVServicePhase string

const (
	KVServicePhasePending     KVServicePhase = "Pending"
	KVServicePhaseProgressing KVServicePhase = "Progressing"
	KVServicePhaseReady       KVServicePhase = "Ready"
	KVServicePhaseDegraded    KVServicePhase = "Degraded"
	KVServicePhaseTerminating KVServicePhase = "Terminating"
)

// NormalizedKVPoolTemplate is the immutable client configuration compiled from
// a KVService storagePools entry. Pool identity and desiredGroups stay outside it.
type NormalizedKVPoolTemplate struct {
	Client KVClientTemplate `json:"client"`
	// +optional
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`
}

// KVPoolSpec is controller-owned normalized client Pool intent. desiredGroups is
// deliberately mutable for in-place scale; all revision-defining fields are immutable.
// +kubebuilder:validation:XValidation:rule="self.kvServiceRef == oldSelf.kvServiceRef && self.poolName == oldSelf.poolName && self.revision == oldSelf.revision && self.template == oldSelf.template",message="only KVPool desiredGroups is mutable"
type KVPoolSpec struct {
	KVServiceRef LocalObjectReference `json:"kvServiceRef"`
	PoolName     string               `json:"poolName"`
	// Revision identifies the immutable Group template generated by this Pool.
	// +kubebuilder:validation:MinLength=1
	Revision string `json:"revision"`
	// +kubebuilder:validation:Minimum=0
	DesiredGroups int32                    `json:"desiredGroups"`
	Template      NormalizedKVPoolTemplate `json:"template"`
}

// +enum
// +kubebuilder:validation:Enum=Pending;Progressing;Ready;Degraded;Terminating
type KVPoolPhase string

const (
	KVPoolPhasePending     KVPoolPhase = "Pending"
	KVPoolPhaseProgressing KVPoolPhase = "Progressing"
	KVPoolPhaseReady       KVPoolPhase = "Ready"
	KVPoolPhaseDegraded    KVPoolPhase = "Degraded"
	KVPoolPhaseTerminating KVPoolPhase = "Terminating"
)

type KVPoolStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	Phase KVPoolPhase `json:"phase,omitempty"`
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Backend",type=string,JSONPath=".spec.backend"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=".status.phase"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type KVService struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              KVServiceSpec   `json:"spec"`
	Status            KVServiceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type KVServiceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []KVService `json:"items"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Desired",type=integer,JSONPath=".spec.desiredGroups"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=".status.phase"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type KVPool struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              KVPoolSpec   `json:"spec"`
	Status            KVPoolStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type KVPoolList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []KVPool `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &KVService{}, &KVServiceList{}, &KVPool{}, &KVPoolList{})
		return nil
	})
}
