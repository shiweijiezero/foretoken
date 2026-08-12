// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the v1alpha1 ModelService custom-resource API.

package v1alpha1

import (
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

// Parallelism defines the execution topology of one ModelGroup.
// +kubebuilder:validation:XValidation:rule="!has(self.dp) || !has(self.ep)",message="parallelism.dp and parallelism.ep are mutually exclusive"
// +kubebuilder:validation:XValidation:rule="!has(self.ep) || self.ep.size % (self.tp * self.pcp) == 0",message="parallelism.ep.size must be divisible by parallelism.tp * parallelism.pcp"
// +kubebuilder:validation:XValidation:rule="self.pcp == 1 || !has(self.dp) || self.dp == 1",message="parallelism.pcp greater than 1 is incompatible with parallelism.dp greater than 1"
// +kubebuilder:validation:XValidation:rule="self.pcp == 1 || !has(self.ep) || self.ep.size == self.tp * self.pcp",message="parallelism.ep.size must equal parallelism.tp * parallelism.pcp when parallelism.pcp is greater than 1"
// +kubebuilder:validation:XValidation:rule="self.pcp == 1 ? self.tp % self.dcp == 0 : self.dcp == 1 || self.dcp == self.pcp || self.dcp == self.tp * self.pcp",message="parallelism.dcp is incompatible with parallelism.tp and parallelism.pcp"
type Parallelism struct {
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	TP int32 `json:"tp,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	PP int32 `json:"pp,omitempty"`

	// +optional
	// +kubebuilder:validation:Minimum=1
	DP *int32 `json:"dp,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	PCP int32 `json:"pcp,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	DCP int32 `json:"dcp,omitempty"`

	// +optional
	EP *ExpertParallelism `json:"ep,omitempty"`
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
// +kubebuilder:validation:XValidation:rule="(has(self.nodes) ? self.nodes : 1) * self.resources.requests.gpu.count == (has(self.parallelism.ep) ? self.parallelism.pp * self.parallelism.ep.size : self.parallelism.pp * self.parallelism.tp * self.parallelism.pcp * (has(self.parallelism.dp) ? self.parallelism.dp : 1))",message="nodes * resources.requests.gpu.count must equal the compiled worker rank count"
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
	Nodes *int32 `json:"nodes,omitempty"`

	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=128
	// +kubebuilder:validation:Pattern="^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$"
	Network string `json:"network,omitempty"`

	Resources   ModelResources `json:"resources"`
	Parallelism Parallelism    `json:"parallelism"`

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

// AutoscalingAlgorithm selects one statically assembled autoscaling configuration.
// +enum
// +kubebuilder:validation:Enum=manual;queue
type AutoscalingAlgorithm string

const (
	AutoscalingAlgorithmManual AutoscalingAlgorithm = "manual"
	AutoscalingAlgorithmQueue  AutoscalingAlgorithm = "queue"
)

// ModelAutoscalingConfig configures controller-owned Group autoscaling.
// +kubebuilder:validation:XValidation:rule="!has(self.minGroups) || !has(self.maxGroups) || self.minGroups <= self.maxGroups",message="autoscaling minGroups must not exceed maxGroups"
// +kubebuilder:validation:XValidation:rule="self.algorithm != 'queue' || has(self.maxGroups)",message="queue autoscaling requires maxGroups"
type ModelAutoscalingConfig struct {
	// +kubebuilder:default=manual
	Algorithm AutoscalingAlgorithm `json:"algorithm"`

	// +optional
	// +kubebuilder:default=0
	// +kubebuilder:validation:Minimum=0
	MinGroups *int32 `json:"minGroups,omitempty"`

	// MaxGroups is required for queue autoscaling. When omitted, manual intent is unbounded.
	// +optional
	// +kubebuilder:validation:Minimum=0
	MaxGroups *int32 `json:"maxGroups,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	TargetQueuePerRoutableGroup *int64 `json:"targetQueuePerRoutableGroup,omitempty"`

	// +optional
	// +kubebuilder:default="5s"
	PollInterval Duration `json:"pollInterval,omitempty"`

	// +optional
	// +kubebuilder:default="15s"
	MetricsMaxAge Duration `json:"metricsMaxAge,omitempty"`

	// Zero disables the corresponding per-evaluation rate limit.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	MaxScaleUpStep *int32 `json:"maxScaleUpStep,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	MaxScaleDownStep *int32 `json:"maxScaleDownStep,omitempty"`
}

// ModelServiceSpec defines the desired state of a model service.
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || !(has(self.replicas) || has(self.nodes) || has(self.resources) || has(self.parallelism) || has(self.maxInputTokens) || has(self.kvCache) || has(self.features))",message="spec.modelPools is mutually exclusive with top-level replicas, nodes, resources, parallelism, maxInputTokens, kvCache, and features"
// +kubebuilder:validation:XValidation:rule="has(self.modelPools) || (has(self.resources) && has(self.parallelism))",message="top-level resources and parallelism are required when spec.modelPools is omitted"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || self.modelPools.all(pool, pool.name != 'default')",message="modelPools name default is reserved for the Quick Start shorthand"
// +kubebuilder:validation:XValidation:rule="has(self.modelPools) || (has(self.resources) && has(self.parallelism) && (has(self.nodes) ? self.nodes : 1) * self.resources.requests.gpu.count == (has(self.parallelism.ep) ? self.parallelism.pp * self.parallelism.ep.size : self.parallelism.pp * self.parallelism.tp * self.parallelism.pcp * (has(self.parallelism.dp) ? self.parallelism.dp : 1)))",message="nodes * resources.requests.gpu.count must equal the compiled worker rank count"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || self.modelPools.all(pool, !has(pool.role) || pool.role == 'aggregate') || (self.modelPools.exists(pool, has(pool.role) && pool.role == 'prefill') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'decode') && self.modelPools.all(pool, has(pool.role) && (pool.role == 'prefill' || pool.role == 'decode'))) || (has(self.ecProfile) && self.modelPools.exists(pool, has(pool.role) && pool.role == 'encoder') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'prefill') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'decode') && self.modelPools.all(pool, has(pool.role) && (pool.role == 'encoder' || pool.role == 'prefill' || pool.role == 'decode')))",message="modelPools must be aggregate-only, complete P/D, or complete E/P/D without aggregate pools"
// +kubebuilder:validation:XValidation:rule="!has(self.ecProfile) || (has(self.modelPools) && self.modelPools.exists(pool, has(pool.role) && pool.role == 'encoder') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'prefill') && self.modelPools.exists(pool, has(pool.role) && pool.role == 'decode') && self.modelPools.all(pool, has(pool.role) && (pool.role == 'encoder' || pool.role == 'prefill' || pool.role == 'decode')))",message="ecProfile requires complete E/P/D modelPools"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || !self.modelPools.exists(pool, has(pool.role) && pool.role == 'encoder') || (size(self.modelPools.filter(pool, has(pool.role) && pool.role == 'encoder')) == 1 && size(self.modelPools.filter(pool, has(pool.role) && pool.role == 'prefill')) == 1 && size(self.modelPools.filter(pool, has(pool.role) && pool.role == 'decode')) == 1)",message="E/P/D modelPools must contain exactly one encoder, prefill, and decode Pool"
// +kubebuilder:validation:XValidation:rule="!has(self.modelPools) || !self.modelPools.exists(pool, has(pool.role) && pool.role == 'encoder') || self.modelPools.filter(pool, has(pool.role) && pool.role == 'encoder').all(e, self.modelPools.filter(pool, has(pool.role) && pool.role == 'prefill').all(p, (has(e.replicas) ? e.replicas : 1) == (has(p.replicas) ? p.replicas : 1)) && self.modelPools.filter(pool, has(pool.role) && pool.role == 'decode').all(d, (has(e.replicas) ? e.replicas : 1) == (has(d.replicas) ? d.replicas : 1)))",message="E/P/D modelPools must have equal encoder, prefill, and decode replica counts"
type ModelServiceSpec struct {
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Model string `json:"model"`

	// ModelRevision pins the model artifact when the platform does not resolve it from a catalog.
	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	ModelRevision string `json:"modelRevision,omitempty"`

	// Tokenizer defaults to model when omitted.
	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Tokenizer string `json:"tokenizer,omitempty"`

	// TokenizerRevision defaults to modelRevision when omitted.
	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=256
	TokenizerRevision string `json:"tokenizerRevision,omitempty"`

	// +kubebuilder:validation:Enum=vllm
	Backend string `json:"backend"`

	// InternalGenerateRequestBodyLimitBytes is the maximum body size accepted by
	// a group-local generate endpoint. It defaults to 64 MiB.
	// +optional
	// +kubebuilder:default=67108864
	// +kubebuilder:validation:Minimum=1048576
	// +kubebuilder:validation:Maximum=268435456
	InternalGenerateRequestBodyLimitBytes *int64 `json:"internalGenerateRequestBodyLimitBytes,omitempty"`

	// Replicas is the number of complete ModelGroups in the default Pool; the compiler defaults it to 1.
	// +optional
	// +kubebuilder:validation:Minimum=0
	Replicas *int32 `json:"replicas,omitempty"`

	// Nodes is the number of physical Kubernetes Nodes used by each ModelGroup; the compiler defaults it to 1.
	// +optional
	// +kubebuilder:validation:Minimum=1
	Nodes *int32 `json:"nodes,omitempty"`

	// +optional
	Resources *ModelResources `json:"resources,omitempty"`

	Timeouts ModelTimeouts `json:"timeouts"`

	// Autoscaling is evaluated by the ModelService controller. Algorithms remain
	// side-effect-free; lifecycle, bounds, rollout, and drain stay core-owned.
	// +optional
	Autoscaling *ModelAutoscalingConfig `json:"autoscaling,omitempty"`

	// +optional
	Parallelism *Parallelism `json:"parallelism,omitempty"`

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

	// ExtraArgs are backend CLI flags shared by every compiled Pool.
	// +optional
	// +listType=atomic
	// +kubebuilder:validation:MaxItems=256
	ExtraArgs []BackendArg `json:"extraArgs,omitempty"`
}

// AutoscalingTargetStatus records the latest auditable decision for one scaling target.
type AutoscalingTargetStatus struct {
	// +kubebuilder:validation:MinLength=1
	ID string `json:"id"`

	// +kubebuilder:validation:Enum=Pool;EPDDomain
	Kind string `json:"kind"`

	// +kubebuilder:validation:Enum=Aggregate;Encoder;Prefill;Decode;EPD
	Role string `json:"role"`

	Algorithm        string      `json:"algorithm"`
	SnapshotID       string      `json:"snapshotID"`
	ObservedAt       metav1.Time `json:"observedAt"`
	ObservationState string      `json:"observationState"`
	Disposition      string      `json:"disposition"`
	Reason           string      `json:"reason"`
	Message          string      `json:"message,omitempty"`
	Direction        string      `json:"direction"`

	// RequestedGroups is the raw advisory target returned by the algorithm.
	RequestedGroups int32 `json:"requestedGroups"`

	// AppliedGroups is the core-constrained target successfully written to all
	// ModelPools represented by this status entry.
	// +kubebuilder:validation:Minimum=0
	AppliedGroups int32 `json:"appliedGroups"`

	// +kubebuilder:validation:Minimum=0
	ReadyGroups int32 `json:"readyGroups"`

	// +kubebuilder:validation:Minimum=0
	RoutableGroups int32 `json:"routableGroups"`
}

// ModelServiceStatus defines the observed state of a model service.
type ModelServiceStatus struct {
	// +optional
	// +kubebuilder:validation:Minimum=0
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`

	// +optional
	// +listType=map
	// +listMapKey=type
	// +kubebuilder:validation:MaxItems=8
	Conditions []metav1.Condition `json:"conditions,omitempty"`

	// Autoscaling contains the latest decision for each Pool or E/P/D domain.
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
