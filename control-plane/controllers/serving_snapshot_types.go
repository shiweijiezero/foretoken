// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the private wire types published in serving discovery snapshots.

package controllers

const servingSnapshotKey = "serving.json"

type servingSnapshot struct {
	Version           uint64                            `json:"version"`
	Models            []servingSnapshotModel            `json:"models"`
	Groups            []servingSnapshotGroup            `json:"groups"`
	PDComponents      []servingSnapshotPDComponent      `json:"pd_components,omitempty"`
	PDPipelineScopes  []servingSnapshotPDPipelineScope  `json:"pd_pipeline_scopes,omitempty"`
	EPDComponents     []servingSnapshotEPDComponent     `json:"epd_components,omitempty"`
	EPDPipelineScopes []servingSnapshotEPDPipelineScope `json:"epd_pipeline_scopes,omitempty"`
}

type servingSnapshotModel struct {
	ServiceUID          string                           `json:"service_uid"`
	Model               string                           `json:"model"`
	Revision            string                           `json:"revision"`
	Tokenizer           string                           `json:"tokenizer"`
	TokenizerRevision   string                           `json:"tokenizer_revision"`
	MaxInputTokens      *int32                           `json:"max_input_tokens,omitempty"`
	Capabilities        []string                         `json:"capabilities,omitempty"`
	AdmissionTargetSets [][]servingSnapshotScalingTarget `json:"admission_target_sets"`
}

type servingSnapshotScalingTarget struct {
	ServiceUID string `json:"service_uid"`
	Name       string `json:"name"`
	UID        string `json:"uid"`
	Kind       string `json:"kind"`
}

type servingSnapshotGroup struct {
	RouteTargetID     string   `json:"route_target_id"`
	ServiceUID        string   `json:"service_uid"`
	PoolUID           string   `json:"pool_uid"`
	PoolName          string   `json:"pool_name"`
	Model             string   `json:"model"`
	Revision          string   `json:"revision"`
	Tokenizer         string   `json:"tokenizer"`
	TokenizerRevision string   `json:"tokenizer_revision"`
	MaxInputTokens    *int32   `json:"max_input_tokens,omitempty"`
	Capabilities      []string `json:"capabilities,omitempty"`
	Endpoint          string   `json:"endpoint"`
	KVScopeID         string   `json:"kv_scope_id"`
	DataParallelSize  int32    `json:"data_parallel_size"`
}

// servingSnapshotPDComponent is one P or D component. Linked route sets express compatibility without P×D materialization.
type servingSnapshotPDComponent struct {
	RouteTargetID            string   `json:"route_target_id"`
	ServiceUID               string   `json:"service_uid"`
	PoolUID                  string   `json:"pool_uid"`
	PoolName                 string   `json:"pool_name"`
	Role                     string   `json:"role"`
	PipelineScopeID          string   `json:"pipeline_scope_id"`
	Model                    string   `json:"model"`
	Revision                 string   `json:"revision"`
	Tokenizer                string   `json:"tokenizer"`
	TokenizerRevision        string   `json:"tokenizer_revision"`
	MaxInputTokens           *int32   `json:"max_input_tokens,omitempty"`
	ProfileName              string   `json:"profile_name"`
	ProfileRevision          string   `json:"profile_revision"`
	Connector                string   `json:"connector"`
	Protocol                 string   `json:"protocol"`
	Capabilities             []string `json:"capabilities"`
	Endpoint                 string   `json:"endpoint"`
	PrefillBootstrapEndpoint string   `json:"prefill_bootstrap_endpoint,omitempty"`
	KVScopeID                string   `json:"kv_scope_id"`
	DataParallelSize         int32    `json:"data_parallel_size"`
}

type servingSnapshotPDPipelineScope struct {
	PipelineScopeID       string   `json:"pipeline_scope_id"`
	PrefillRouteTargetIDs []string `json:"prefill_route_target_ids"`
	DecodeRouteTargetIDs  []string `json:"decode_route_target_ids"`
}

// servingSnapshotEPDComponent is one component of an atomic 1E:1P:1D triplet.
type servingSnapshotEPDComponent struct {
	RouteTargetID            string   `json:"route_target_id"`
	ServiceUID               string   `json:"service_uid"`
	PoolUID                  string   `json:"pool_uid"`
	PoolName                 string   `json:"pool_name"`
	Role                     string   `json:"role"`
	PipelineScopeID          string   `json:"pipeline_scope_id"`
	Model                    string   `json:"model"`
	Revision                 string   `json:"revision"`
	Tokenizer                string   `json:"tokenizer"`
	TokenizerRevision        string   `json:"tokenizer_revision"`
	MaxInputTokens           *int32   `json:"max_input_tokens,omitempty"`
	ProfileName              string   `json:"profile_name,omitempty"`
	ProfileRevision          string   `json:"profile_revision,omitempty"`
	Connector                string   `json:"connector,omitempty"`
	Protocol                 string   `json:"protocol,omitempty"`
	ECProfileName            string   `json:"ec_profile_name,omitempty"`
	ECProfileRevision        string   `json:"ec_profile_revision,omitempty"`
	ECConnector              string   `json:"ec_connector,omitempty"`
	Capabilities             []string `json:"capabilities"`
	Endpoint                 string   `json:"endpoint"`
	PrefillBootstrapEndpoint string   `json:"prefill_bootstrap_endpoint,omitempty"`
	KVScopeID                string   `json:"kv_scope_id"`
	DataParallelSize         int32    `json:"data_parallel_size"`
}

type servingSnapshotEPDPipelineScope struct {
	PipelineScopeID      string `json:"pipeline_scope_id"`
	EncoderRouteTargetID string `json:"encoder_route_target_id"`
	PrefillRouteTargetID string `json:"prefill_route_target_id"`
	DecodeRouteTargetID  string `json:"decode_route_target_id"`
}
