// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the durable Kubernetes-native asynchronous video generation API.
package v1alpha1

import (
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

// VideoTaskServiceReference selects one existing ModelService in the task namespace.
type VideoTaskServiceReference struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// VideoInputFile identifies an input file already available to the worker.
type VideoInputFile struct {
	// +kubebuilder:validation:MinLength=1
	Field string `json:"field"`
	// +kubebuilder:validation:MinLength=1
	Path string `json:"path"`
	// +kubebuilder:validation:MinLength=1
	ContentType string `json:"contentType"`
}

// VideoRequest is the backend-neutral request passed to the worker Job.
type VideoRequest struct {
	// +kubebuilder:validation:MinLength=1
	Task string `json:"task"`
	// +kubebuilder:validation:MinLength=1
	Prompt string `json:"prompt"`
	// +kubebuilder:validation:Minimum=1
	Width int32 `json:"width"`
	// +kubebuilder:validation:Minimum=1
	Height int32 `json:"height"`
	// +kubebuilder:validation:Minimum=1
	NumFrames int32 `json:"numFrames"`
	// +kubebuilder:validation:Minimum=1
	FPS int32 `json:"fps"`
	// +kubebuilder:validation:Minimum=1
	NumInferenceSteps int32 `json:"numInferenceSteps"`
	// +optional
	AspectRatio string `json:"aspectRatio,omitempty"`
	// +optional
	FlowShift *float64 `json:"flowShift,omitempty"`
	// +optional
	AudioFlowShift *float64 `json:"audioFlowShift,omitempty"`
	// +optional
	Seed *int64 `json:"seed,omitempty"`
	// +optional
	// +listType=atomic
	FrameIndices []int32 `json:"frameIndices,omitempty"`
	// +optional
	// +listType=atomic
	InputFiles []VideoInputFile `json:"inputFiles,omitempty"`
}

// VideoWorkerSpec defines the execution contract for the controller-created Job.
type VideoWorkerSpec struct {
	// Image is the worker image implementing the Foretoken video task contract.
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
	// Endpoint is the internal video model-server endpoint used by the worker.
	// +kubebuilder:validation:MinLength=1
	Endpoint string `json:"endpoint"`
	// OutputClaimName is a namespace-local PVC mounted at OutputPath.
	// +kubebuilder:validation:MinLength=1
	OutputClaimName string `json:"outputClaimName"`
	// +kubebuilder:validation:MinLength=1
	OutputPath string `json:"outputPath"`
}

// VideoTaskSpec requests one durable asynchronous video generation.
// +kubebuilder:validation:XValidation:rule="self.frontendUID == oldSelf.frontendUID && self.modelServiceRef == oldSelf.modelServiceRef && self.request == oldSelf.request && self.worker == oldSelf.worker",message="video task frontend, target, request and worker are immutable"
// +kubebuilder:validation:XValidation:rule="!has(oldSelf.inputsReady) || !oldSelf.inputsReady || (has(self.inputsReady) && self.inputsReady)",message="ready task inputs cannot become unready"
// +kubebuilder:validation:XValidation:rule="!has(oldSelf.cancelRequested) || !oldSelf.cancelRequested || (has(self.cancelRequested) && self.cancelRequested)",message="cancellation cannot be withdrawn"
type VideoTaskSpec struct {
	// FrontendUID binds this task to the FrontendService that admitted it.
	// +kubebuilder:validation:MinLength=1
	FrontendUID     string                    `json:"frontendUID"`
	ModelServiceRef VideoTaskServiceReference `json:"modelServiceRef"`
	Request         VideoRequest              `json:"request"`
	Worker          VideoWorkerSpec           `json:"worker"`
	// +optional
	CancelRequested bool `json:"cancelRequested,omitempty"`
	// InputsReady is set after task-local input files are durable on the shared PVC.
	// +optional
	InputsReady bool `json:"inputsReady,omitempty"`
}

// VideoExecutionPlan is controller-owned recovery state persisted before Job creation.
type VideoExecutionPlan struct {
	// +optional
	Model string `json:"model,omitempty"`
	// +optional
	ServiceUID string `json:"serviceUID,omitempty"`
	// +optional
	ServingGeneration int64 `json:"servingGeneration,omitempty"`
	// +optional
	Revisions        []ServingPoolRevision `json:"revisions,omitempty"`
	JobName          string                `json:"jobName"`
	OutputClaimName  string                `json:"outputClaimName"`
	OutputPath       string                `json:"outputPath"`
	WorkerImage      string                `json:"workerImage"`
	RetentionSeconds int64                 `json:"retentionSeconds"`
	TimeoutSeconds   int64                 `json:"timeoutSeconds"`
}

// VideoArtifactReference identifies the worker-produced artifact on retained storage.
type VideoArtifactReference struct {
	ClaimName string `json:"claimName"`
	Path      string `json:"path"`
}

// VideoTaskStatus publishes durable Job observation and artifact identity.
type VideoTaskStatus struct {
	// +optional
	Plan *VideoExecutionPlan `json:"plan,omitempty"`
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	// +kubebuilder:validation:Enum=Pending;Starting;Running;Succeeded;Failed;Cancelled
	Phase string `json:"phase,omitempty"`
	// +optional
	Reason string `json:"reason,omitempty"`
	// +optional
	Message string `json:"message,omitempty"`
	// +optional
	JobName string `json:"jobName,omitempty"`
	// +optional
	JobUID string `json:"jobUID,omitempty"`
	// +optional
	JobCreationRequested bool `json:"jobCreationRequested,omitempty"`
	// +optional
	StartedAt *metav1.Time `json:"startedAt,omitempty"`
	// +optional
	FinishedAt *metav1.Time `json:"finishedAt,omitempty"`
	// +optional
	Artifact *VideoArtifactReference `json:"artifact,omitempty"`
	// ExpiresAt is persisted once a terminal result is published.
	// +optional
	ExpiresAt *metav1.Time `json:"expiresAt,omitempty"`
	// CleanupJobName keeps cleanup execution distinct from inference.
	// +optional
	CleanupJobName string `json:"cleanupJobName,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Phase",type=string,JSONPath=".status.phase"
// +kubebuilder:printcolumn:name="Job",type=string,JSONPath=".status.jobName"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// VideoTask is a retained asynchronous video generation request.
type VideoTask struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              VideoTaskSpec   `json:"spec"`
	Status            VideoTaskStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// VideoTaskList contains asynchronous video generation requests.
type VideoTaskList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []VideoTask `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &VideoTask{}, &VideoTaskList{})
		return nil
	})
}
