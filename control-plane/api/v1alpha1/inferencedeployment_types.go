// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// DeploymentUIDLabel binds resources and runtime consumers to one deployment lifecycle.
const DeploymentUIDLabel = "inference.foretoken.io/deployment-uid"

// DeploymentResourceReference identifies user intent belonging to one deployment instance.
// Resources use the deployment's namespace; their own APIs continue to own their configuration.
type DeploymentResourceReference struct {
	APIVersion string `json:"apiVersion"`
	Kind       string `json:"kind"`
	Name       string `json:"name"`
}

// InferenceDeploymentSpec selects the controller and membership of an independently operated instance.
type InferenceDeploymentSpec struct {
	// UpdateID requests the current platform defaults after applications have been published.
	// Repeated requests do not replace an unchanged controller Pod template.
	// +optional
	UpdateID string `json:"updateID,omitempty"`

	// ControllerSourceRevision selects a completed control-plane publication at the platform origin.
	// An empty selection uses the installed platform application.
	// +optional
	ControllerSourceRevision string `json:"controllerSourceRevision,omitempty"`

	// Resources records the submitted root resources for status, endpoint discovery, and cleanup.
	// +optional
	// +listType=map
	// +listMapKey=apiVersion
	// +listMapKey=kind
	// +listMapKey=name
	Resources []DeploymentResourceReference `json:"resources,omitempty"`
}

// InferenceDeploymentStatus reports the selected controller independently of model readiness.
type InferenceDeploymentStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// ControllerRevision identifies the ready controller Deployment by UID and generation.
	// Service application selection uses this identity, not the CLI's update request ID.
	// +optional
	ControllerRevision string `json:"controllerRevision,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced,shortName=ideploy
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// InferenceDeployment owns an instance's controller lifecycle without owning its Kubernetes namespace.
// The platform keeps that controller running until the instance's services finish retirement.
type InferenceDeployment struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              InferenceDeploymentSpec   `json:"spec,omitempty"`
	Status            InferenceDeploymentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// InferenceDeploymentList contains independently operated deployment instances.
type InferenceDeploymentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []InferenceDeployment `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &InferenceDeployment{}, &InferenceDeploymentList{})
		return nil
	})
}
