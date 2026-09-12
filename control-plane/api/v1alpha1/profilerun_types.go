// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the one-shot, service-owned diagnostic capture API.
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// ProfileServiceReference selects one existing ModelService in the run namespace.
type ProfileServiceReference struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// ProfileRunSpec requests a bounded capture without changing serving configuration.
// +kubebuilder:validation:XValidation:rule="self.modelServiceRef == oldSelf.modelServiceRef && self.duration == oldSelf.duration && self.engine == oldSelf.engine",message="capture target, engine and duration are immutable"
// +kubebuilder:validation:XValidation:rule="oldSelf.action == 'Capture' || self.action == oldSelf.action || (oldSelf.action == 'Finish' && self.action == 'Cancel')",message="capture actions cannot move backwards"
// +kubebuilder:validation:XValidation:rule="duration(self.duration) >= duration('1ms')",message="capture duration must be at least 1ms"
type ProfileRunSpec struct {
	ModelServiceRef ProfileServiceReference `json:"modelServiceRef"`
	// Engine selects the native profiler prepared by the diagnostic runtime.
	// +kubebuilder:validation:Enum=pytorch
	Engine   string   `json:"engine"`
	Duration Duration `json:"duration"`
	// +kubebuilder:default=Capture
	// +kubebuilder:validation:Enum=Capture;Finish;Cancel
	Action string `json:"action"`
}

// ProfileArtifactReference identifies retained storage independently of the runtime Pod.
type ProfileArtifactReference struct {
	ClaimName string `json:"claimName"`
	Path      string `json:"path"`
}

// ProfileParticipant fixes the runtime that may execute this capture.
// Replacement Pods and processes cannot inherit an in-flight operation.
type ProfileParticipant struct {
	GroupName string `json:"groupName"`
	GroupUID  string `json:"groupUID"`
	PodName   string `json:"podName"`
	PodUID    string `json:"podUID"`
	RuntimeID string `json:"runtimeID"`
	Endpoint  string `json:"endpoint"`
}

// ProfileExecutionPlan is controller-owned recovery state, persisted before any start.
type ProfileExecutionPlan struct {
	ServiceUID        string                `json:"serviceUID"`
	ServingGeneration int64                 `json:"servingGeneration"`
	Revisions         []ServingPoolRevision `json:"revisions"`
	ArtifactClaim     string                `json:"artifactClaim"`
	Participants      []ProfileParticipant  `json:"participants"`
}

// ProfileRunStatus publishes observed capture progress, not benchmark completion.
type ProfileRunStatus struct {
	// Plan is fixed once selected; reconciliation never retargets replacement runtimes.
	// +optional
	Plan *ProfileExecutionPlan `json:"plan,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	// +kubebuilder:validation:Enum=Pending;Starting;Capturing;Stopping;Succeeded;Failed;Cancelled
	Phase string `json:"phase,omitempty"`
	// +optional
	Reason string `json:"reason,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	Participants int32 `json:"participants,omitempty"`
	// +optional
	CompletedParticipants int32 `json:"completedParticipants,omitempty"`
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// +optional
	FinishedAt *metav1.Time `json:"finishedAt,omitempty"`
	// +optional
	Artifact *ProfileArtifactReference `json:"artifact,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=".status.phase"
// +kubebuilder:printcolumn:name="Service",type=string,JSONPath=".spec.modelServiceRef.name"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// ProfileRun is a retained execution request; the controller owns stop and recovery.
type ProfileRun struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              ProfileRunSpec   `json:"spec"`
	Status            ProfileRunStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ProfileRunList contains diagnostic capture requests.
type ProfileRunList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ProfileRun `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &ProfileRun{}, &ProfileRunList{})
		return nil
	})
}
