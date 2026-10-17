// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Package core defines the fixed autoscaling pipeline contracts.
package core

import "time"

type Direction string

const (
	DirectionUp   Direction = "Up"
	DirectionDown Direction = "Down"
	DirectionHold Direction = "Hold"
)

type TargetKind string

const TargetPool TargetKind = "Pool"

type TargetRole string

const (
	RoleAggregate TargetRole = "Aggregate"
	RoleEncoder   TargetRole = "Encoder"
	RolePrefill   TargetRole = "Prefill"
	RoleDecode    TargetRole = "Decode"
)

type TargetID struct {
	ServiceNamespace string
	ServiceName      string
	ServiceUID       string
	Name             string
	UID              string
	Kind             TargetKind
	Role             TargetRole
}

type MetricsState string

const (
	MetricsUnavailable MetricsState = "Unavailable"
	MetricsFresh       MetricsState = "Fresh"
	MetricsStale       MetricsState = "Stale"
)

type MetricsWindow struct {
	Start       time.Time // Local collection start.
	End         time.Time // Oldest source sample included in the snapshot.
	CollectedAt time.Time // Local collection completion.
	Samples     int32
	Complete    bool
}

// MetricsSnapshot contains the complete backend-neutral metrics available for one evaluation.
type MetricsSnapshot struct {
	State           MetricsState
	Window          MetricsWindow
	WaitingRequests int64
	RunningRequests int64
	ActiveRequests  int64
	// EngineLoads preserves complete scheduler observations instead of averaging ranks.
	EngineLoads []EngineLoad
}

// EngineLoad is one controller-attributed scheduler sample consumed by token-load policies.
// Counts describe engine work, including queued demand, rather than physical cache occupancy.
type EngineLoad struct {
	GroupUID                string
	DataParallelRank        uint32
	QueuedPrefillTokens     uint64
	ScheduledDecodeKVTokens uint64
	QueuedDecodeKVTokens    uint64
	ContextLength           uint64
	KVCapacityTokens        uint64
}

type ReplicaState struct {
	BaselineReplicas     int32
	RequestedReplicas    int32
	ReadyReplicas        int32
	RoutableReplicas     int32
	PendingReplicas      int32
	ProvisioningReplicas int32
	DrainingReplicas     int32
	TerminatingReplicas  int32
	FailedReplicas       int32
	Transitioning        bool
}

type ReplicaLimits struct {
	MinReplicas int32
	MaxReplicas int32
}

type ScalingSnapshot struct {
	Target      TargetID
	EvaluatedAt time.Time
	Replicas    ReplicaState
	Limits      ReplicaLimits
	Metrics     MetricsSnapshot
}
