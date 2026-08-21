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

const (
	TargetPool             TargetKind = "Pool"
	TargetEPDPipelineScope TargetKind = "EPDPipelineScope"
)

type TargetRole string

const (
	RoleAggregate TargetRole = "Aggregate"
	RoleEncoder   TargetRole = "Encoder"
	RolePrefill   TargetRole = "Prefill"
	RoleDecode    TargetRole = "Decode"
	RoleEPD       TargetRole = "EPD"
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
type ObservationState string

const (
	ObservationUnavailable ObservationState = "Unavailable"
	ObservationFresh       ObservationState = "Fresh"
	ObservationStale       ObservationState = "Stale"
)

type ObservationWindow struct {
	Start       time.Time // Local collection start.
	End         time.Time // Oldest source sample included in the aggregate.
	CollectedAt time.Time // Local collection completion.
	Samples     int32
	Complete    bool
}
type DemandObservation struct {
	State          ObservationState
	Window         ObservationWindow
	QueueRequests  int64
	ActiveRequests int64
}
type CapacityState struct {
	BaselineGroups     int32
	RequestedGroups    int32
	ReadyGroups        int32
	RoutableGroups     int32
	PendingGroups      int32
	ProvisioningGroups int32
	DrainingGroups     int32
	TerminatingGroups  int32
	FailedGroups       int32
	Transitioning      bool
}
type CapacityLimits struct {
	MinGroups          int32
	MaxGroups          int32
	MaxScaleUpGroups   int32
	MaxScaleDownGroups int32
}
type ScalingSnapshot struct {
	ID          string
	Target      TargetID
	EvaluatedAt time.Time
	Capacity    CapacityState
	Limits      CapacityLimits
	Observation DemandObservation
}
