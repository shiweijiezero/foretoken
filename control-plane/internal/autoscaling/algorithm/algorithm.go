// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the side-effect-free contract for community autoscaling algorithms.

package algorithm

import (
	"context"
	"time"
)

// Direction describes the final capacity movement selected by core.
type Direction string

const (
	DirectionUp   Direction = "Up"
	DirectionDown Direction = "Down"
	DirectionHold Direction = "Hold"
)

// TargetKind identifies the atomic capacity unit presented to an algorithm.
type TargetKind string

const (
	TargetPool      TargetKind = "Pool"
	TargetEPDDomain TargetKind = "EPDDomain"
)

// TargetRole exposes serving topology without leaking Kubernetes API types.
type TargetRole string

const (
	RoleAggregate TargetRole = "Aggregate"
	RoleEncoder   TargetRole = "Encoder"
	RolePrefill   TargetRole = "Prefill"
	RoleDecode    TargetRole = "Decode"
	RoleEPD       TargetRole = "EPD"
)

// TargetID identifies one scaling target. Name is local to the ModelService;
// UIDs are the authoritative identities when they are available.
type TargetID struct {
	ServiceNamespace string
	ServiceName      string
	ServiceUID       string
	Name             string
	UID              string
	Kind             TargetKind
	Role             TargetRole
}

// SnapshotRef fences a recommendation to the immutable input that produced it.
type SnapshotRef struct {
	ID             string
	PolicyRevision string
}

// ObservationState distinguishes observed zero values from unavailable data.
type ObservationState string

const (
	ObservationUnavailable ObservationState = "Unavailable"
	ObservationFresh       ObservationState = "Fresh"
	ObservationStale       ObservationState = "Stale"
)

// ObservationWindow describes when and how completely a signal was observed.
type ObservationWindow struct {
	Start       time.Time
	End         time.Time
	CollectedAt time.Time
	Samples     int32
	Complete    bool
}

// DemandObservation contains typed, Pool-attributed demand signals.
type DemandObservation struct {
	State          ObservationState
	Window         ObservationWindow
	QueueRequests  int64
	ActiveRequests int64
}

// CapacityState describes declarative, requested, and observed Group lifecycle.
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

// CapacityLimits contains core-owned bounds and per-evaluation rate limits.
type CapacityLimits struct {
	MinGroups        int32
	MaxGroups        int32
	MaxScaleUpStep   int32
	MaxScaleDownStep int32
}

// Snapshot is the immutable input presented to one scaling algorithm.
type Snapshot struct {
	Target      TargetID
	Ref         SnapshotRef
	EvaluatedAt time.Time
	Capacity    CapacityState
	Limits      CapacityLimits
	Observation DemandObservation
}

// RecommendationDisposition defines the only algorithm outcomes core accepts.
type RecommendationDisposition string

const (
	RecommendationApply            RecommendationDisposition = "Apply"
	RecommendationHold             RecommendationDisposition = "Hold"
	RecommendationInsufficientData RecommendationDisposition = "InsufficientData"
)

// RecommendationReason is a stable machine-readable diagnostic code.
type RecommendationReason string

const (
	ReasonManualIntent           RecommendationReason = "ManualIntent"
	ReasonQueuePressure          RecommendationReason = "QueuePressure"
	ReasonIdle                   RecommendationReason = "Idle"
	ReasonStable                 RecommendationReason = "Stable"
	ReasonAtMinimum              RecommendationReason = "AtMinimum"
	ReasonAtMaximum              RecommendationReason = "AtMaximum"
	ReasonObservationUnavailable RecommendationReason = "ObservationUnavailable"
	ReasonObservationStale       RecommendationReason = "ObservationStale"
	ReasonObservationIncomplete  RecommendationReason = "ObservationIncomplete"
	ReasonTransitionInProgress   RecommendationReason = "TransitionInProgress"
)

// Recommendation is advisory. Kubernetes actuation remains core-owned.
type Recommendation struct {
	Target        TargetID
	SnapshotID    string
	Disposition   RecommendationDisposition
	DesiredGroups int32
	Reason        RecommendationReason
	Message       string
}

// Algorithm computes one recommendation without Kubernetes or telemetry clients.
type Algorithm interface {
	Name() string
	Recommend(context.Context, Snapshot) (Recommendation, error)
}
