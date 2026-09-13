// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the user-owned RuntimeCache API and its controller-managed PVC status.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// RuntimeCacheAccessMode selects the Kubernetes PVC access mode.
// +enum
// +kubebuilder:validation:Enum=ReadWriteOnce;ReadWriteMany
type RuntimeCacheAccessMode string

const (
	RuntimeCacheAccessModeReadWriteOnce RuntimeCacheAccessMode = "ReadWriteOnce"
	RuntimeCacheAccessModeReadWriteMany RuntimeCacheAccessMode = "ReadWriteMany"
)

// RuntimeCacheRetentionPolicy controls PVC cleanup when a RuntimeCache is deleted.
// +enum
// +kubebuilder:validation:Enum=Delete;Retain
type RuntimeCacheRetentionPolicy string

const (
	RuntimeCacheRetentionPolicyDelete RuntimeCacheRetentionPolicy = "Delete"
	RuntimeCacheRetentionPolicyRetain RuntimeCacheRetentionPolicy = "Retain"
)

// RuntimeCacheSpec defines one platform-managed persistent cache volume.
// +kubebuilder:validation:XValidation:rule="!has(self.initialSize) || quantity(self.initialSize).compareTo(quantity('0')) > 0",message="initialSize must be positive"
// +kubebuilder:validation:XValidation:rule="!has(self.maxSize) || quantity(self.maxSize).compareTo(quantity('0')) > 0",message="maxSize must be positive"
// +kubebuilder:validation:XValidation:rule="!has(self.maxSize) || !has(self.initialSize) || quantity(self.maxSize).compareTo(quantity(self.initialSize)) > 0",message="maxSize must be greater than initialSize"
// +kubebuilder:validation:XValidation:rule="has(self.directory) || has(self.initialSize)",message="initialSize is required when directory is not set"
// +kubebuilder:validation:XValidation:rule="has(self.storageClassName) == has(oldSelf.storageClassName) && (!has(self.storageClassName) || self.storageClassName == oldSelf.storageClassName) && has(self.initialSize) == has(oldSelf.initialSize) && (!has(self.initialSize) || self.initialSize == oldSelf.initialSize) && self.accessMode == oldSelf.accessMode && self.retentionPolicy == oldSelf.retentionPolicy && has(self.directory) == has(oldSelf.directory) && (!has(self.directory) || self.directory == oldSelf.directory)",message="only maxSize may change"
// +kubebuilder:validation:XValidation:rule="!has(oldSelf.maxSize) || (has(self.maxSize) && quantity(self.maxSize).compareTo(quantity(oldSelf.maxSize)) >= 0)",message="maxSize cannot be removed or decreased"
// +kubebuilder:validation:XValidation:rule="!has(self.directory) || (!has(self.maxSize) && !has(self.initialSize) && !has(self.storageClassName))",message="directory cannot be combined with PVC size or storageClassName"
type RuntimeCacheSpec struct {
	// Directory is a deployment-root-relative directory for a static hostPath PV.
	// Declaring it means the resolved path is already shared at the same location on every target node.
	// The deploy command resolves the path but never uploads its contents or installs shared storage.
	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=4096
	Directory string `json:"directory,omitempty"`

	// +optional
	StorageClassName string `json:"storageClassName,omitempty"`

	// InitialSize is the first PVC request for dynamic storage. Directory mode uses
	// an internal bookkeeping capacity when this field is omitted.
	// +optional
	InitialSize ResourceQuantity `json:"initialSize,omitempty"`

	// MaxSize enables automatic expansion when set and may only be increased.
	// +optional
	MaxSize ResourceQuantity `json:"maxSize,omitempty"`

	// +optional
	// +kubebuilder:default=ReadWriteMany
	AccessMode RuntimeCacheAccessMode `json:"accessMode,omitempty"`

	// +optional
	// +kubebuilder:default=Retain
	RetentionPolicy RuntimeCacheRetentionPolicy `json:"retentionPolicy,omitempty"`
}

// RuntimeCachePhase summarizes the managed PVC lifecycle.
// +enum
// +kubebuilder:validation:Enum=Pending;Ready;Resizing;Degraded;Terminating
type RuntimeCachePhase string

const (
	RuntimeCachePhasePending     RuntimeCachePhase = "Pending"
	RuntimeCachePhaseReady       RuntimeCachePhase = "Ready"
	RuntimeCachePhaseResizing    RuntimeCachePhase = "Resizing"
	RuntimeCachePhaseDegraded    RuntimeCachePhase = "Degraded"
	RuntimeCachePhaseTerminating RuntimeCachePhase = "Terminating"
)

// RuntimeCacheStatus publishes the managed claim and its observed capacity.
type RuntimeCacheStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// +optional
	Phase RuntimeCachePhase `json:"phase,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`

	// +optional
	ClaimName string `json:"claimName,omitempty"`

	// +optional
	Capacity ResourceQuantity `json:"capacity,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Claim",type=string,JSONPath=".status.claimName"
// +kubebuilder:printcolumn:name="Capacity",type=string,JSONPath=".status.capacity"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
type RuntimeCache struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              RuntimeCacheSpec   `json:"spec"`
	Status            RuntimeCacheStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true
type RuntimeCacheList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []RuntimeCache `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &RuntimeCache{}, &RuntimeCacheList{})
		return nil
	})
}
