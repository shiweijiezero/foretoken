// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Defines the controller-owned v1alpha1 ModelGroup custom-resource API.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// ModelGroupAccelerator defines accelerator capacity for each runtime member Pod.
type ModelGroupAccelerator struct {
	// Type is the resolved platform accelerator type.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern="^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$"
	Type string `json:"type"`

	// CountPerMember is the number of accelerators assigned to one member Pod.
	// +kubebuilder:validation:Minimum=1
	CountPerMember int32 `json:"countPerMember"`
}

// ModelGroupSpec is an immutable execution contract compiled by the Pool controller.
// +kubebuilder:validation:XValidation:rule="self == oldSelf",message="ModelGroup spec is immutable"
// +kubebuilder:validation:XValidation:rule="self.memberCount == self.nodeCount",message="memberCount must equal nodeCount in v1alpha1"
// +kubebuilder:validation:XValidation:rule="self.nodeCount * self.accelerator.countPerMember == self.parallelism.pp * self.parallelism.tp * self.parallelism.pcp * self.parallelism.dp",message="accelerator capacity must equal the compiled worker rank count"
type ModelGroupSpec struct {
	ModelPoolRef LocalObjectReference `json:"modelPoolRef"`

	// Revision identifies the immutable Pool template revision this Group realizes.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
	Revision string `json:"revision"`

	// Ordinal is unique within one Pool revision.
	// +kubebuilder:validation:Minimum=0
	Ordinal int32 `json:"ordinal"`

	Role ModelRole `json:"role"`

	// NodeCount is the number of physical Kubernetes Nodes used by this Group.
	// +kubebuilder:validation:Minimum=1
	NodeCount int32 `json:"nodeCount"`

	// MemberCount is the number of runtime member Pods in this Group.
	// +kubebuilder:validation:Minimum=1
	MemberCount int32 `json:"memberCount"`

	Parallelism CompiledParallelism `json:"parallelism"`

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
