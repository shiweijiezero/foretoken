// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines controller-owned Mooncake client Groups.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// KVGroupDisk is the resolved SSD offload claim for one client Group.
type KVGroupDisk struct {
	// +optional
	StorageClassName string          `json:"storageClassName,omitempty"`
	Size             ByteQuantity    `json:"size"`
	RetentionPolicy  RetentionPolicy `json:"retentionPolicy"`
}

// KVGroupClientConfig is the resolved immutable Mooncake client workload input.
// Disk is mandatory: this first standalone Store profile enables SSD offload.
type KVGroupClientConfig struct {
	Image string `json:"image"`
	// +kubebuilder:validation:Enum=rdma
	Protocol string `json:"protocol"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port      int32       `json:"port"`
	Resources KVResources `json:"resources"`
	// +kubebuilder:validation:MinLength=1
	RDMAResourceName string `json:"rdmaResourceName"`
	// +kubebuilder:validation:Minimum=1
	RDMAResourceCount   int32        `json:"rdmaResourceCount"`
	MemoryCapacityBytes ByteQuantity `json:"memoryCapacityBytes"`
	Disk                KVGroupDisk  `json:"disk"`
	// +optional
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`
}

// KVGroupSpec is immutable, resolved client intent materialized from a KVPool.
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="KVGroup spec is immutable"
type KVGroupSpec struct {
	KVPoolRef LocalObjectReference `json:"kvPoolRef"`
	// +kubebuilder:validation:MinLength=1
	Revision string `json:"revision"`
	// +kubebuilder:validation:Minimum=0
	Ordinal int32 `json:"ordinal"`
	// MasterServiceDNS is the namespaced ClusterIP Service DNS name resolved by KVPool.
	// +kubebuilder:validation:MinLength=1
	MasterServiceDNS string `json:"masterServiceDNS"`
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	MasterRPCPort int32               `json:"masterRPCPort"`
	Client        KVGroupClientConfig `json:"client"`
	Timeouts      KVTimeouts          `json:"timeouts"`
}

// +enum
// +kubebuilder:validation:Enum=Pending;Provisioning;Ready;Degraded;Draining;Terminating
type KVGroupPhase string

const (
	KVGroupPhasePending      KVGroupPhase = "Pending"
	KVGroupPhaseProvisioning KVGroupPhase = "Provisioning"
	KVGroupPhaseReady        KVGroupPhase = "Ready"
	KVGroupPhaseDegraded     KVGroupPhase = "Degraded"
	KVGroupPhaseDraining     KVGroupPhase = "Draining"
	KVGroupPhaseTerminating  KVGroupPhase = "Terminating"
)

// KVGroupStatus reports requested capacity and Kubernetes infrastructure only.
// It never represents Mooncake registration or usable Store capacity.
type KVGroupStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	Phase KVGroupPhase `json:"phase,omitempty"`
	// +optional
	RequestedMemoryCapacityBytes ByteQuantity `json:"requestedMemoryCapacityBytes,omitempty"`
	// +optional
	RequestedDiskCapacityBytes ByteQuantity `json:"requestedDiskCapacityBytes,omitempty"`
	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Revision",type=string,JSONPath=".spec.revision"
// +kubebuilder:printcolumn:name="Ordinal",type=integer,JSONPath=".spec.ordinal"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=".status.phase"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type KVGroup struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              KVGroupSpec   `json:"spec"`
	Status            KVGroupStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type KVGroupList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []KVGroup `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &KVGroup{}, &KVGroupList{})
		return nil
	})
}
