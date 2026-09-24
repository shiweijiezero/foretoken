// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the v1alpha1 FrontendService custom-resource API.

package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// FrontendTimeouts defines client-facing request budgets.
// +kubebuilder:validation:XValidation:rule="duration(self.streamIdle) <= duration(self.request)",message="timeouts.streamIdle must not exceed timeouts.request"
type FrontendTimeouts struct {
	// +kubebuilder:validation:Pattern="^([0-9]+(s|m|h))+$"
	Request Duration `json:"request"`

	// +kubebuilder:validation:Pattern="^([0-9]+(s|m|h))+$"
	StreamIdle Duration `json:"streamIdle"`
}

// RouterAlgorithm names one compiled routing algorithm.
type RouterAlgorithm string

const (
	// DefaultRouterFilter is applied by the API when a Filter is omitted.
	DefaultRouterFilter RouterAlgorithm = "allow_all"
	// DefaultRouterScorer is applied by the API when a Scorer is omitted.
	DefaultRouterScorer RouterAlgorithm = "kv_least_loaded"
	// DefaultRouterPicker is applied by the API when a Picker is omitted.
	DefaultRouterPicker RouterAlgorithm = "gamble_sampling"
)

// RouterStage selects one compiled algorithm and its algorithm-owned parameters.
type RouterStage struct {
	// +kubebuilder:validation:MinLength=1
	Algorithm RouterAlgorithm `json:"algorithm"`

	// +optional
	// +kubebuilder:validation:Type=object
	// +kubebuilder:pruning:PreserveUnknownFields
	Parameters *runtime.RawExtension `json:"parameters,omitempty"`
}

// RouterPipeline selects each independently composable routing algorithm stage.
type RouterPipeline struct {
	// +kubebuilder:default={algorithm:allow_all,parameters:{}}
	Filter RouterStage `json:"filter"`

	// +kubebuilder:default={algorithm:kv_least_loaded,parameters:{}}
	Scorer RouterStage `json:"scorer"`

	// +kubebuilder:default={algorithm:gamble_sampling,parameters:{}}
	Picker RouterStage `json:"picker"`
}

// FrontendServiceSpec defines the desired state of a frontend service.
type FrontendServiceSpec struct {
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	Replicas *int32 `json:"replicas,omitempty"`

	Resources FrontendResources `json:"resources"`
	Timeouts  FrontendTimeouts  `json:"timeouts"`

	// Observability selects frontend-scoped alerts, including shared HTTP failures.
	// +optional
	Observability *FrontendObservability `json:"observability,omitempty"`

	// +optional
	// +kubebuilder:default={filter:{algorithm:allow_all},scorer:{algorithm:kv_least_loaded},picker:{algorithm:gamble_sampling}}
	RouterPipeline RouterPipeline `json:"routerPipeline,omitempty"`

	// Hostname is required when the platform exposes frontends through a Gateway.
	// +optional
	// +kubebuilder:validation:MaxLength=253
	// +kubebuilder:validation:Pattern="^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?(\\.[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?)*$"
	Hostname string `json:"hostname,omitempty"`
}

// FrontendServiceStatus defines the observed state of a frontend service.
type FrontendServiceStatus struct {
	// +optional
	// +kubebuilder:validation:Minimum=0
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// Last successfully persisted versioned routing configuration version. It survives ConfigMap recreation.
	// +optional
	// +kubebuilder:validation:Minimum=0
	ServingSnapshotVersion uint64 `json:"servingSnapshotVersion,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	// +kubebuilder:validation:MaxItems=8
	Conditions []metav1.Condition `json:"conditions,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Hostname",type=string,JSONPath=".spec.hostname"
// +kubebuilder:printcolumn:name="Replicas",type=integer,JSONPath=".spec.replicas"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// FrontendService is the user-owned frontend serving API.
type FrontendService struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              FrontendServiceSpec   `json:"spec"`
	Status            FrontendServiceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// FrontendServiceList contains FrontendService resources.
type FrontendServiceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []FrontendService `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &FrontendService{}, &FrontendServiceList{})
		return nil
	})
}
