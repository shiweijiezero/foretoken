// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the v1alpha1 ModelService custom-resource API.

package v1alpha1

import (
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

const (
	DefaultInternalGenerateRequestBodyLimitBytes int64 = 64 * 1024 * 1024
	MinInternalGenerateRequestBodyLimitBytes     int64 = 1 * 1024 * 1024
	MaxInternalGenerateRequestBodyLimitBytes     int64 = 256 * 1024 * 1024
)

// ModelTimeouts defines ModelService lifecycle budgets.
type ModelTimeouts struct {
	Startup Duration `json:"startup"`
	Drain   Duration `json:"drain"`
}

// ExpertParallelism defines expert-parallel execution for one ModelGroup.
type ExpertParallelism struct {
	// +kubebuilder:validation:Minimum=1
	Size int32 `json:"size"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	Backend string `json:"backend,omitempty"`

	// +optional
	// +kubebuilder:default=false
	EPLB bool `json:"eplb,omitempty"`
}

// KVCache defines typed KV-cache placement intent for one execution Pool.
// +kubebuilder:validation:XValidation:rule="!(has(self.offload) && has(self.mooncakeStore))",message="kvCache.offload and kvCache.mooncakeStore are mutually exclusive"
type KVCache struct {
	// +optional
	Offload *KVOffload `json:"offload,omitempty"`

	// +optional
	MooncakeStore *MooncakeStore `json:"mooncakeStore,omitempty"`
}

// KVOffload defines local CPU and optionally filesystem-backed KV offload.
type KVOffload struct {
	// CPUCacheSize is the CPU tier capacity available to vLLM.
	// +kubebuilder:validation:MinLength=1
	CPUCacheSize ResourceQuantity `json:"cpuCacheSize"`

	// +optional
	Filesystem bool `json:"filesystem,omitempty"`
}

// KVServiceReference identifies a same-namespace KVService by user-facing name only.
type KVServiceReference struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// MooncakeStore selects either an external platform profile or a Foretoken-owned KVService.
// +kubebuilder:validation:XValidation:rule="has(self.profile) != has(self.kvServiceRef)",message="exactly one of profile or kvServiceRef is required"
type MooncakeStore struct {
	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	Profile string `json:"profile,omitempty"`
	// +optional
	KVServiceRef *KVServiceReference `json:"kvServiceRef,omitempty"`
}

// ECProfileReference selects a platform-maintained encoder/prefill transfer profile.
// It deliberately contains no connector options or module paths.
type ECProfileReference struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	Profile string `json:"profile"`
}

// ModelPoolTemplate defines one user-owned execution Pool: a homogeneous set
// of ModelGroups sharing the same role, network, resources, and parallelism.
// The controller instantiates it as a ModelPool owned by the ModelService.
type ModelPoolTemplate struct {
	// Name is the stable identity of this Pool within one ModelService.
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern="^[a-z0-9]([-a-z0-9]*[a-z0-9])?$"
	Name string `json:"name"`

	// +optional
	// +kubebuilder:default=aggregate
	Role ModelRole `json:"role,omitempty"`

	// Replicas is the number of complete ModelGroups in this Pool.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	Replicas *int32 `json:"replicas,omitempty"`

	// Nodes is the number of physical Kubernetes Nodes used by each ModelGroup.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=1
	Nodes *int32 `json:"nodes,omitempty"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern="^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$"
	Network string `json:"network,omitempty"`

	Resources ModelResources `json:"resources"`

	// EngineArgs replaces service-level native options for this Pool when supplied.
	// +optional
	EngineArgs *EngineArguments `json:"engineArgs,omitempty"`

	// MaxInputTokens is the prompt admission limit for requests routed to this Pool.
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxInputTokens *int32 `json:"maxInputTokens,omitempty"`

	// +optional
	KVCache *KVCache `json:"kvCache,omitempty"`

	// Features declares explicit opt-in capabilities for this Pool.
	// +optional
	Features *ModelFeatures `json:"features,omitempty"`
}

// ModelAutoscalingAlgorithmConfig selects a built-in stage and its optional algorithm-owned parameters.
type ModelAutoscalingAlgorithmConfig struct {
	// Algorithm identifies a stage implementation compiled into the controller.
	// +kubebuilder:validation:MinLength=1
	Algorithm string `json:"algorithm"`

	// Parameters overrides the selected implementation's defaults when supplied.
	// +optional
	// +kubebuilder:validation:Type=object
	// +kubebuilder:pruning:PreserveUnknownFields
	Parameters *apiextensionsv1.JSON `json:"parameters,omitempty"`
}

// ModelAutoscalingConfig configures user-visible service replica autoscaling.
// Automatic scaling always maintains at least one service replica; scale-to-zero is not supported.
// +kubebuilder:validation:XValidation:rule="self.minReplicas <= self.maxReplicas",message="autoscaling minReplicas must not exceed maxReplicas"
type ModelAutoscalingConfig struct {
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	MinReplicas int32 `json:"minReplicas,omitempty"`

	// +kubebuilder:validation:Minimum=1
	MaxReplicas int32 `json:"maxReplicas"`

	// Trigger may be omitted to use periodic evaluation every five seconds.
	// +optional
	Trigger *ModelAutoscalingAlgorithmConfig `json:"trigger,omitempty"`

	Decision ModelAutoscalingAlgorithmConfig `json:"decision"`

	// +optional
	Adjustment *ModelAutoscalingAlgorithmConfig `json:"adjustment,omitempty"`
}

// ModelSource selects how model and tokenizer identifiers are resolved.
// +kubebuilder:validation:Enum=local;hf;modelscope
type ModelSource string

const (
	ModelSourceLocal      ModelSource = "local"
	ModelSourceHF         ModelSource = "hf"
	ModelSourceModelScope ModelSource = "modelscope"
)

// ProfilingConfig selects instrumentation prepared when model processes start.
// Captures are still requested separately through ProfileRun.
type ProfilingConfig struct {
	// +kubebuilder:validation:Enum=pytorch;nsight
	Engine string `json:"engine"`
}

// InferenceParameters contains common model-execution choices shared by every Pool.
type InferenceParameters struct {
	// MaxModelLen limits the combined prompt and generated sequence length.
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxModelLen *int32 `json:"maxModelLen,omitempty"`

	// DType selects the model weight and activation data type supported by the engine.
	// +optional
	// +kubebuilder:validation:MinLength=1
	DType string `json:"dtype,omitempty"`

	// Quantization selects the engine's weight quantization method.
	// +optional
	// +kubebuilder:validation:MinLength=1
	Quantization string `json:"quantization,omitempty"`

	// KVCacheDType selects the data type used for the engine's KV cache.
	// +optional
	// +kubebuilder:validation:MinLength=1
	KVCacheDType string `json:"kvCacheDType,omitempty"`

	// GPUMemoryUtilization is the fraction of device memory available to each engine instance.
	// +optional
	// +kubebuilder:validation:Minimum=0
	// +kubebuilder:validation:ExclusiveMinimum=true
	// +kubebuilder:validation:Maximum=1
	GPUMemoryUtilization *float64 `json:"gpuMemoryUtilization,omitempty"`

	// MaxNumSeqs limits the sequences scheduled in one engine iteration.
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxNumSeqs *int32 `json:"maxNumSeqs,omitempty"`

	// MaxNumBatchedTokens limits the tokens scheduled in one engine iteration.
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxNumBatchedTokens *int32 `json:"maxNumBatchedTokens,omitempty"`

	// EnforceEager disables graph capture when true; omission preserves the engine default.
	// +optional
	EnforceEager *bool `json:"enforceEager,omitempty"`

	// SpeculativeDecoding is the complete native speculative configuration for the selected backend.
	// It replaces the corresponding engineArgs option when present.
	// +optional
	SpeculativeDecoding *EngineArguments `json:"speculativeDecoding,omitempty"`
}

// ModelServiceSpec defines the desired state of a model service.
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || !(has(self.replicas) || has(self.nodes) || has(self.resources) || has(self.maxInputTokens) || has(self.kvCache) || has(self.features))",message="spec.modelPools is mutually exclusive with top-level replicas, nodes, resources, maxInputTokens, kvCache, and features"
// +kubebuilder:validation:XValidation:rule="has(self.modelPools) || has(self.resources)",message="top-level resources are required when spec.modelPools is omitted"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || self.modelPools.all(pool, pool.name != 'default')",message="modelPools name default is reserved for the Quick Start shorthand"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || self.modelPools.all(pool, !has(pool.role) || pool.role == 'aggregate') || (self.modelPools.exists(pool, has(pool.role) && pool.role == 'prefill') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'decode') && self.modelPools.all(pool, has(pool.role) && (pool.role == 'prefill' || pool.role == 'decode'))) || (has(self.ecProfile) && self.modelPools.exists(pool, has(pool.role) && pool.role == 'encoder') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'prefill') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'decode') && self.modelPools.all(pool, has(pool.role) && (pool.role == 'encoder' || pool.role == 'prefill' || pool.role == 'decode')))",message="modelPools must be aggregate-only, complete P/D, or complete E/P/D without aggregate pools"
// +kubebuilder:validation:XValidation:rule="!has(self.ecProfile) || (has(self.modelPools) && self.modelPools.exists(pool, has(pool.role) && pool.role == 'encoder') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'prefill') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'decode') && self.modelPools.all(pool, has(pool.role) && (pool.role == 'encoder' || pool.role == 'prefill' || pool.role == 'decode')))",message="ecProfile requires complete E/P/D modelPools"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || !self.modelPools.exists(pool, has(pool.role) && pool.role == 'encoder') || (size(self.modelPools.filter(pool, has(pool.role) && pool.role == 'encoder')) == 1 && size(self.modelPools.filter(pool, has(pool.role) && pool.role == 'prefill')) == 1 && size(self.modelPools.filter(pool, has(pool.role) && pool.role == 'decode')) == 1)",message="E/P/D modelPools must contain exactly one encoder, prefill, and decode Pool"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || !self.modelPools.exists(pool, has(pool.role) && pool.role == 'encoder') || self.modelPools.filter(pool, has(pool.role) && pool.role == 'encoder').all(e, self.modelPools.filter(pool, has(pool.role) && pool.role == 'prefill').all(p, (has(e.replicas) ? e.replicas : 1) == (has(p.replicas) ? p.replicas : 1)) && self.modelPools.filter(pool, has(pool.role) && pool.role == 'decode').all(d, (has(e.replicas) ? e.replicas : 1) == (has(d.replicas) ? d.replicas : 1)))",message="E/P/D modelPools must have equal encoder, prefill, and decode replica counts"
type ModelServiceSpec struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Model string `json:"model"`

	// Source defaults to Hugging Face Hub when omitted.
	// +optional
	// +kubebuilder:default=hf
	Source ModelSource `json:"source,omitempty"`

	// Tokenizer defaults to model when omitted and uses the same source.
	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Tokenizer string `json:"tokenizer,omitempty"`

	// +kubebuilder:validation:Enum=vllm
	Backend string `json:"backend"`

	// Common execution choices apply to every Pool; explicit values override EngineArgs.
	InferenceParameters `json:",inline"`

	// InternalGenerateRequestBodyLimitBytes is the maximum body size accepted by
	// a group-local generate endpoint. It defaults to 64 MiB.
	// +optional
	// +kubebuilder:default=67108864
	// +kubebuilder:validation:Minimum=1048576
	// +kubebuilder:validation:Maximum=268435456
	InternalGenerateRequestBodyLimitBytes *int64 `json:"internalGenerateRequestBodyLimitBytes,omitempty"`

	// Replicas is the baseline service replica count; the compiler defaults it to 1.
	// When autoscaling is configured, its minReplicas and maxReplicas bound capacity from the first reconcile.
	// +optional
	// +kubebuilder:validation:Minimum=0
	Replicas *int32 `json:"replicas,omitempty"`

	// Nodes is the number of physical Kubernetes Nodes used by each ModelGroup; the compiler defaults it to 1.
	// +optional
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=1
	Nodes *int32 `json:"nodes,omitempty"`

	// +optional
	Resources *ModelResources `json:"resources,omitempty"`

	Timeouts ModelTimeouts `json:"timeouts"`

	// Profiling prepares a profiler for every model Pool; omission preserves PyTorch support.
	// Changing the prepared engine rolls out new model processes but does not start a capture.
	// +optional
	Profiling *ProfilingConfig `json:"profiling,omitempty"`

	// Observability selects model-scoped alerts independently of serving and autoscaling.
	// +optional
	Observability *ModelObservability `json:"observability,omitempty"`

	// Autoscaling is evaluated by the ModelService controller. Algorithms remain
	// side-effect-free; lifecycle, bounds, rollout, and drain stay core-owned.
	// +optional
	Autoscaling *ModelAutoscalingConfig `json:"autoscaling,omitempty"`

	// MaxInputTokens is the prompt admission limit for requests routed to the default Pool.
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxInputTokens *int32 `json:"maxInputTokens,omitempty"`

	// KVCache is the typed cache-placement intent for the Quick Start shorthand.
	// +optional
	KVCache *KVCache `json:"kvCache,omitempty"`

	// Features declares explicit opt-in capabilities for the default Pool.
	// +optional
	Features *ModelFeatures `json:"features,omitempty"`

	// ECProfile selects the platform-maintained EC runtime for a complete E/P/D topology.
	// +optional
	ECProfile *ECProfileReference `json:"ecProfile,omitempty"`

	// ModelPools defines up to 32 advanced execution Pools instead of the top-level shorthand.
	// +optional
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:MinItems=1
	// +kubebuilder:validation:MaxItems=32
	ModelPools []ModelPoolTemplate `json:"modelPools,omitempty"`

	// EngineArgs uses the selected backend's native option names without leading --.
	// Explicit common fields in spec take precedence over matching engine options.
	// +optional
	EngineArgs EngineArguments `json:"engineArgs,omitempty"`
}

// AutoscalingStageStatus describes one named pipeline stage result.
type AutoscalingStageStatus struct {
	Algorithm   string `json:"algorithm"`
	Disposition string `json:"disposition"`
	Reason      string `json:"reason"`
	Message     string `json:"message,omitempty"`
}

// AutoscalingDecisionStatus records the Decision stage replica recommendation.
type AutoscalingDecisionStatus struct {
	AutoscalingStageStatus `json:",inline"`

	// +kubebuilder:validation:Minimum=0
	DesiredReplicas int32 `json:"desiredReplicas"`
}

// AutoscalingAdjustmentStatus records the replica count after stabilization and rate limiting.
type AutoscalingAdjustmentStatus struct {
	AutoscalingStageStatus `json:",inline"`

	// +kubebuilder:validation:Minimum=0
	AdjustedReplicas int32 `json:"adjustedReplicas"`
}

// AutoscalingConstraintStatus describes a lifecycle constraint that changed the adjusted capacity.
type AutoscalingConstraintStatus struct {
	Reason  string `json:"reason"`
	Message string `json:"message,omitempty"`
}

// AutoscalingTargetStatus is the latest complete autoscaling evaluation for one scaling target.
type AutoscalingTargetStatus struct {
	// +kubebuilder:validation:MinLength=1
	ID string `json:"id"`

	// +kubebuilder:validation:Enum=Pool;EPDPipelineScope
	Kind string `json:"kind"`

	// +kubebuilder:validation:Enum=Aggregate;Encoder;Prefill;Decode;EPD
	Role string `json:"role"`

	EvaluatedAt metav1.Time `json:"evaluatedAt"`

	// ObservationEndAt is the oldest source sample included in this evaluation.
	// It is absent for fixed manual capacity.
	// +optional
	ObservationEndAt *metav1.Time `json:"observationEndAt,omitempty"`

	ObservationState string `json:"observationState"`

	// +optional
	Trigger *AutoscalingStageStatus `json:"trigger,omitempty"`

	Decision AutoscalingDecisionStatus `json:"decision"`

	Adjustment AutoscalingAdjustmentStatus `json:"adjustment"`

	// Constraint is set when hard bounds or lifecycle state override adjusted capacity.
	// +optional
	Constraint *AutoscalingConstraintStatus `json:"constraint,omitempty"`

	Direction string `json:"direction"`

	// AppliedReplicas is the platform-resolved replica count successfully written to all
	// ModelPools represented by this status entry.
	// +kubebuilder:validation:Minimum=0
	AppliedReplicas int32 `json:"appliedReplicas"`

	// +kubebuilder:validation:Minimum=0
	ReadyReplicas int32 `json:"readyReplicas"`

	// +kubebuilder:validation:Minimum=0
	RoutableReplicas int32 `json:"routableReplicas"`
}

// ServingPoolRevision selects one prepared ModelPool cohort for the active service generation.
type ServingPoolRevision struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	PoolName string `json:"poolName"`

	// +kubebuilder:validation:MinLength=1
	PoolUID string `json:"poolUID"`

	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=63
	Revision string `json:"revision"`
}

// ModelServiceStatus defines the observed state of a model service.
type ModelServiceStatus struct {
	// +optional
	// +kubebuilder:validation:Minimum=0
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// ServingGeneration is the ModelService generation whose complete Pool revision set is admitted to routing.
	// +optional
	// +kubebuilder:validation:Minimum=0
	ServingGeneration int64 `json:"servingGeneration,omitempty"`

	// ServingPoolRevisions is replaced atomically after every required Pool has prepared a compatible cohort.
	// +optional
	// +listType=map
	// +listMapKey=poolName
	// +kubebuilder:validation:MaxItems=32
	ServingPoolRevisions []ServingPoolRevision `json:"servingPoolRevisions,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	// +kubebuilder:validation:MaxItems=8
	Conditions []metav1.Condition `json:"conditions,omitempty"`

	// Autoscaling contains the latest decision for each Pool or linked E/P/D processing unit.
	// +optional
	// +listType=map
	// +listMapKey=id
	// +kubebuilder:validation:MaxItems=32
	Autoscaling []AutoscalingTargetStatus `json:"autoscaling,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:subresource:status
// +kubebuilder:printcolumn:name="Backend",type=string,JSONPath=".spec.backend"
// +kubebuilder:printcolumn:name="Replicas",type=integer,JSONPath=".spec.replicas"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"

// ModelService is the user-owned model serving API.
type ModelService struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              ModelServiceSpec   `json:"spec"`
	Status            ModelServiceStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// ModelServiceList contains ModelService resources.
type ModelServiceList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []ModelService `json:"items"`
}

func init() {
	SchemeBuilder.Register(func(scheme *runtime.Scheme) error {
		scheme.AddKnownTypes(GroupVersion, &ModelService{}, &ModelServiceList{})
		return nil
	})
}
