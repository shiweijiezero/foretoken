// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the private configuration contract published to serving runtimes.

package servingconfig

import (
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// Settings groups process-local rules that share the snapshot publication boundary.
type Settings struct {
	RouterPipeline        inferencev1alpha1.RouterPipeline `json:"router_pipeline"`
	RequestTimeoutSeconds int64                            `json:"request_timeout_seconds"`
	StreamIdleSeconds     int64                            `json:"stream_idle_seconds"`
	LogLevel              string                           `json:"log_level"`
}

type Snapshot struct {
	Settings          Settings             `json:"settings"`
	Version           uint64               `json:"version"`
	FrontendInstances []string             `json:"frontend_instances"`
	BackendInstances  []string             `json:"backend_instances"`
	Models            []Model              `json:"models"`
	Admission         map[string]Admission `json:"admission"`
	Groups            []Group              `json:"groups"`
	PDComponents      []PDComponent        `json:"pd_components,omitempty"`
	PDPipelineScopes  []PDPipelineScope    `json:"pd_pipeline_scopes,omitempty"`
	EPDComponents     []EPDComponent       `json:"epd_components,omitempty"`
	EPDPipelineScopes []EPDPipelineScope   `json:"epd_pipeline_scopes,omitempty"`
}

// Admission contains the effective per-model settings consumed by the frontend.
type Admission struct {
	MaxWaitingRequests *uint32                      `json:"maxWaitingRequests,omitempty"`
	QueueTimeout       inferencev1alpha1.Duration   `json:"queueTimeout,omitempty"`
	RoleRules          []inferencev1alpha1.RoleRule `json:"roleRules,omitempty"`
}

// Model identifies one configured service behind a public model, independently of ready capacity.
type Model struct {
	ServiceName           string                                  `json:"service_name,omitempty"`
	ServiceGeneration     int64                                   `json:"service_generation"`
	ServiceUID            string                                  `json:"service_uid"`
	Model                 string                                  `json:"model"`
	Source                inferencev1alpha1.ModelSource           `json:"source"`
	Revision              string                                  `json:"revision"`
	Tokenizer             string                                  `json:"tokenizer"`
	TokenizerRevision     string                                  `json:"tokenizer_revision"`
	PrefixCacheIsolation  *bool                                   `json:"prefix_cache_isolation,omitempty"`
	RouterPipeline        *inferencev1alpha1.ModelRouterPipeline  `json:"router_pipeline,omitempty"`
	SelectedPoolRevisions []inferencev1alpha1.ServingPoolRevision `json:"selected_pool_revisions,omitempty"`
	Topology              string                                  `json:"topology"`
	Capabilities          []string                                `json:"capabilities,omitempty"`
	AdmissionTargetSets   [][]ScalingTarget                       `json:"admission_target_sets"`
}

type ScalingTarget struct {
	ServiceUID string `json:"service_uid"`
	Name       string `json:"name"`
	UID        string `json:"uid"`
	Kind       string `json:"kind"`
}

type Group struct {
	RouteTargetID        string                        `json:"route_target_id"`
	ServiceUID           string                        `json:"service_uid"`
	PoolUID              string                        `json:"pool_uid"`
	PoolName             string                        `json:"pool_name"`
	Model                string                        `json:"model"`
	Source               inferencev1alpha1.ModelSource `json:"source"`
	Revision             string                        `json:"revision"`
	Tokenizer            string                        `json:"tokenizer"`
	TokenizerRevision    string                        `json:"tokenizer_revision"`
	PrefixCacheIsolation *bool                         `json:"prefix_cache_isolation,omitempty"`
	MaxInputTokens       *int32                        `json:"max_input_tokens,omitempty"`
	Capabilities         []string                      `json:"capabilities,omitempty"`
	Endpoint             string                        `json:"endpoint"`
	KVScopeID            string                        `json:"kv_scope_id"`
	KVLookupScope        string                        `json:"kv_lookup_scope,omitempty"`
	DataParallelSize     int32                         `json:"data_parallel_size"`
}

// Parallelism is the controller-owned execution topology used by KV transfer.
type Parallelism struct {
	TP  int32 `json:"tp"`
	PP  int32 `json:"pp"`
	DP  int32 `json:"dp"`
	PCP int32 `json:"pcp"`
	DCP int32 `json:"dcp"`
	EP  bool  `json:"ep"`
}

// PDComponent is one P or D component. Linked route sets express compatibility without P×D materialization.
type PDComponent struct {
	RouteTargetID            string                        `json:"route_target_id"`
	ServiceUID               string                        `json:"service_uid"`
	PoolUID                  string                        `json:"pool_uid"`
	PoolName                 string                        `json:"pool_name"`
	Role                     string                        `json:"role"`
	PipelineScopeID          string                        `json:"pipeline_scope_id"`
	Model                    string                        `json:"model"`
	Source                   inferencev1alpha1.ModelSource `json:"source"`
	Revision                 string                        `json:"revision"`
	Tokenizer                string                        `json:"tokenizer"`
	TokenizerRevision        string                        `json:"tokenizer_revision"`
	PrefixCacheIsolation     *bool                         `json:"prefix_cache_isolation,omitempty"`
	MaxInputTokens           *int32                        `json:"max_input_tokens,omitempty"`
	ProfileName              string                        `json:"profile_name"`
	ProfileRevision          string                        `json:"profile_revision"`
	Connector                string                        `json:"connector"`
	Protocol                 string                        `json:"protocol"`
	Capabilities             []string                      `json:"capabilities"`
	Endpoint                 string                        `json:"endpoint"`
	PrefillBootstrapEndpoint string                        `json:"prefill_bootstrap_endpoint,omitempty"`
	KVScopeID                string                        `json:"kv_scope_id"`
	KVLookupScope            string                        `json:"kv_lookup_scope,omitempty"`
	DataParallelSize         int32                         `json:"data_parallel_size"`
	Parallelism              Parallelism                   `json:"parallelism"`
}

type PDPipelineScope struct {
	PipelineScopeID       string   `json:"pipeline_scope_id"`
	PrefillRouteTargetIDs []string `json:"prefill_route_target_ids"`
	DecodeRouteTargetIDs  []string `json:"decode_route_target_ids"`
}

// EPDComponent is one route in an E/P/D compatibility scope.
type EPDComponent struct {
	RouteTargetID            string                        `json:"route_target_id"`
	ServiceUID               string                        `json:"service_uid"`
	PoolUID                  string                        `json:"pool_uid"`
	PoolName                 string                        `json:"pool_name"`
	Role                     string                        `json:"role"`
	Model                    string                        `json:"model"`
	Source                   inferencev1alpha1.ModelSource `json:"source"`
	Revision                 string                        `json:"revision"`
	Tokenizer                string                        `json:"tokenizer"`
	TokenizerRevision        string                        `json:"tokenizer_revision"`
	PrefixCacheIsolation     *bool                         `json:"prefix_cache_isolation,omitempty"`
	MaxInputTokens           *int32                        `json:"max_input_tokens,omitempty"`
	Capabilities             []string                      `json:"capabilities"`
	Endpoint                 string                        `json:"endpoint"`
	PrefillBootstrapEndpoint string                        `json:"prefill_bootstrap_endpoint,omitempty"`
	KVScopeID                string                        `json:"kv_scope_id"`
	KVLookupScope            string                        `json:"kv_lookup_scope,omitempty"`
	DataParallelSize         int32                         `json:"data_parallel_size"`
	Parallelism              Parallelism                   `json:"parallelism"`
}

type EPDPipelineScope struct {
	PipelineScopeID       string   `json:"pipeline_scope_id"`
	EncoderRouteTargetIDs []string `json:"encoder_route_target_ids"`
	PrefillRouteTargetIDs []string `json:"prefill_route_target_ids"`
	DecodeRouteTargetIDs  []string `json:"decode_route_target_ids"`
}
