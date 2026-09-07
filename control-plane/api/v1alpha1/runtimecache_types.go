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

// RuntimeCacheExpansionMode selects whether the controller may grow the managed PVC.
// +enum
// +kubebuilder:validation:Enum=Automatic;Disabled
type RuntimeCacheExpansionMode string

const (
	RuntimeCacheExpansionAutomatic RuntimeCacheExpansionMode = "Automatic"
	RuntimeCacheExpansionDisabled  RuntimeCacheExpansionMode = "Disabled"
)

// RuntimeCacheExpansion defines bounded automatic growth from filesystem observations.
// +kubebuilder:validation:XValidation:rule="self.mode == 'Disabled' || (has(self.reserve) && quantity(self.reserve).compareTo(quantity('0')) > 0 && has(self.maxSize) && quantity(self.maxSize).compareTo(quantity('0')) > 0)",message="automatic expansion requires positive reserve and maxSize"
// +kubebuilder:validation:XValidation:rule="self.mode == 'Automatic' || (!has(self.reserve) && !has(self.maxSize))",message="disabled expansion cannot set reserve or maxSize"
// +kubebuilder:validation:XValidation:rule="self.mode != 'Automatic' || quantity(self.reserve).compareTo(quantity(self.maxSize)) <= 0",message="reserve must not exceed maxSize"
type RuntimeCacheExpansion struct {
	Mode RuntimeCacheExpansionMode `json:"mode"`

	// Reserve is the minimum free filesystem capacity maintained for active workloads.
	// +optional
	Reserve ResourceQuantity `json:"reserve,omitempty"`

	// MaxSize bounds the PVC request after automatic expansion.
	// +optional
	MaxSize ResourceQuantity `json:"maxSize,omitempty"`
}

// RuntimeCacheSpec defines one platform-managed persistent cache volume.
// +kubebuilder:validation:XValidation:rule="quantity(self.initialSize).compareTo(quantity('0')) > 0",message="initialSize must be positive"
// +kubebuilder:validation:XValidation:rule="!has(self.expansion) || self.expansion.mode != 'Automatic' || quantity(self.expansion.maxSize).compareTo(quantity(self.initialSize)) >= 0",message="maxSize must not be smaller than initialSize"
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="RuntimeCache storage settings are immutable"
type RuntimeCacheSpec struct {
	// +optional
	StorageClassName string `json:"storageClassName,omitempty"`

	InitialSize ResourceQuantity `json:"initialSize"`

	// +optional
	// +kubebuilder:default=ReadWriteMany
	AccessMode RuntimeCacheAccessMode `json:"accessMode,omitempty"`

	// +optional
	// +kubebuilder:default=Retain
	RetentionPolicy RuntimeCacheRetentionPolicy `json:"retentionPolicy,omitempty"`

	// +optional
	Expansion *RuntimeCacheExpansion `json:"expansion,omitempty"`
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
