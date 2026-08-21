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
	// +kubebuilder:validation:Maximum=1
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

// AutoscalingAlgorithm selects a statically linked autoscaling decision algorithm.
// +kubebuilder:validation:Enum=manual;queue;threshold
type AutoscalingAlgorithm string

const (
	AutoscalingAlgorithmManual    AutoscalingAlgorithm = "manual"
	AutoscalingAlgorithmQueue     AutoscalingAlgorithm = "queue"
	AutoscalingAlgorithmThreshold AutoscalingAlgorithm = "threshold"
)

// AutoscalingTriggerAlgorithm selects when an automatic scaling algorithm is evaluated.
// +kubebuilder:validation:Enum=periodic;watermark
type AutoscalingTriggerAlgorithm string

const (
	AutoscalingTriggerAlgorithmPeriodic  AutoscalingTriggerAlgorithm = "periodic"
	AutoscalingTriggerAlgorithmWatermark AutoscalingTriggerAlgorithm = "watermark"
)

// ModelAutoscalingTriggerConfig configures evaluation triggering for automatic scaling.
// +kubebuilder:validation:XValidation:rule="!has(self.lowQueuePerRoutableGroup) || !has(self.highQueuePerRoutableGroup) || self.lowQueuePerRoutableGroup <= self.highQueuePerRoutableGroup",message="autoscaling trigger lowQueuePerRoutableGroup must not exceed highQueuePerRoutableGroup"
type ModelAutoscalingTriggerConfig struct {
	// +optional
	// +kubebuilder:default=periodic
	Algorithm AutoscalingTriggerAlgorithm `json:"algorithm,omitempty"`

	// +optional
	// +kubebuilder:default="5s"
	Interval Duration `json:"interval,omitempty"`

	// LowQueuePerRoutableGroup fires a watermark evaluation for idle capacity when
	// queue is at or below this per-routable-Group level and no requests are active.
	// +optional
	// +kubebuilder:default=0
	// +kubebuilder:validation:Minimum=0
	LowQueuePerRoutableGroup *int64 `json:"lowQueuePerRoutableGroup,omitempty"`

	// HighQueuePerRoutableGroup fires a watermark evaluation only when queue
	// strictly exceeds this per-routable-Group level.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	HighQueuePerRoutableGroup *int64 `json:"highQueuePerRoutableGroup,omitempty"`
}

// AutoscalingAdjustmentAlgorithm selects how a desired capacity is adjusted before resolution.
// +kubebuilder:validation:Enum=direct;step
type AutoscalingAdjustmentAlgorithm string

const (
	AutoscalingAdjustmentAlgorithmDirect AutoscalingAdjustmentAlgorithm = "direct"
	AutoscalingAdjustmentAlgorithmStep   AutoscalingAdjustmentAlgorithm = "step"
)

// ModelAutoscalingAdjustmentConfig configures the adjustment stage.
type ModelAutoscalingAdjustmentConfig struct {
	// +optional
	// +kubebuilder:default=step
	Algorithm AutoscalingAdjustmentAlgorithm `json:"algorithm,omitempty"`

	// Zero disables the upward per-evaluation step limit.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	MaxScaleUpGroups *int32 `json:"maxScaleUpGroups,omitempty"`

	// Zero disables the downward per-evaluation step limit.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	MaxScaleDownGroups *int32 `json:"maxScaleDownGroups,omitempty"`
}

// ModelAutoscalingConfig configures controller-owned Group autoscaling.
// +kubebuilder:validation:XValidation:rule="!has(self.minGroups) || !has(self.maxGroups) || self.minGroups <= self.maxGroups",message="autoscaling minGroups must not exceed maxGroups"
// +kubebuilder:validation:XValidation:rule="self.algorithm == 'manual' || has(self.maxGroups)",message="autoscaling maxGroups is required unless algorithm is manual"
type ModelAutoscalingConfig struct {
	// +kubebuilder:default=manual
	Algorithm AutoscalingAlgorithm `json:"algorithm"`

	// +optional
	// +kubebuilder:default=0
	// +kubebuilder:validation:Minimum=0
	MinGroups *int32 `json:"minGroups,omitempty"`

	// MaxGroups is required for automatic autoscaling. When omitted, manual intent is unbounded.
	// +optional
	// +kubebuilder:validation:Minimum=0
	MaxGroups *int32 `json:"maxGroups,omitempty"`

	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	TargetQueuePerRoutableGroup *int64 `json:"targetQueuePerRoutableGroup,omitempty"`

	// ScaleUpQueue is the queue-depth threshold above which threshold adds one Group.
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=0
	ScaleUpQueue *int64 `json:"scaleUpQueue,omitempty"`

	// Trigger controls when an automatic algorithm receives a fresh observation.
	// It is ignored by manual autoscaling.
	// +optional
	Trigger *ModelAutoscalingTriggerConfig `json:"trigger,omitempty"`

	// +optional
	// +kubebuilder:default="15s"
	MetricsMaxAge Duration `json:"metricsMaxAge,omitempty"`

	// Adjustment modifies a scaling desired capacity before platform decision resolution.
	// Manual intent always uses direct adjustment.
	// +optional
	Adjustment *ModelAutoscalingAdjustmentConfig `json:"adjustment,omitempty"`
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

	// Tokenizer defaults to model when omitted.
	// +optional
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:MaxLength=1024
	Tokenizer string `json:"tokenizer,omitempty"`

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
	// +kubebuilder:validation:Maximum=1
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

	// ExtraArgs are inference-engine CLI flags shared by every compiled Pool.
	// +optional
	// +listType=atomic
	// +kubebuilder:validation:MaxItems=256
	ExtraArgs []BackendArg `json:"extraArgs,omitempty"`
}

// AutoscalingTargetStatus records the latest auditable decision for one scaling target.
type AutoscalingTargetStatus struct {
	// +kubebuilder:validation:MinLength=1
	ID string `json:"id"`

	// +kubebuilder:validation:Enum=Pool;EPDPipelineScope
	Kind string `json:"kind"`

	// +kubebuilder:validation:Enum=Aggregate;Encoder;Prefill;Decode;EPD
	Role string `json:"role"`

	Algorithm           string      `json:"algorithm"`
	AdjustmentAlgorithm string      `json:"adjustmentAlgorithm"`
	TriggerAlgorithm    string      `json:"triggerAlgorithm,omitempty"`
	SnapshotID          string      `json:"snapshotID"`
	ObservedAt          metav1.Time `json:"observedAt"`
	ObservationState    string      `json:"observationState"`
	Disposition         string      `json:"disposition"`
	Reason              string      `json:"reason"`
	Message             string      `json:"message,omitempty"`
	TriggerDisposition  string      `json:"triggerDisposition,omitempty"`
	TriggerReason       string      `json:"triggerReason,omitempty"`
	TriggerMessage      string      `json:"triggerMessage,omitempty"`
	Direction           string      `json:"direction"`

	// DesiredGroups is the capacity calculated by the scaling decision algorithm.
	DesiredGroups int32 `json:"desiredGroups"`

	// AdjustmentReason and AdjustmentMessage explain desired-capacity adjustment.
	AdjustmentReason  string `json:"adjustmentReason,omitempty"`
	AdjustmentMessage string `json:"adjustmentMessage,omitempty"`

	// AdjustedGroups is the capacity proposed by the adjustment decision.
	AdjustedGroups int32 `json:"adjustedGroups"`

	// AppliedGroups is the platform-resolved target successfully written to all
	// ModelPools represented by this status entry.
	// +kubebuilder:validation:Minimum=0
	AppliedGroups int32 `json:"appliedGroups"`

	// +kubebuilder:validation:Minimum=0
	ReadyGroups int32 `json:"readyGroups"`

	// +kubebuilder:validation:Minimum=0
	RoutableGroups int32 `json:"routableGroups"`
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
