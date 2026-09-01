// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

type RecommendationState string

const (
	RecommendationAvailable        RecommendationState = "Available"
	RecommendationInsufficientData RecommendationState = "InsufficientData"
)

type RecommendationReason string

const (
	RecommendationReasonManualIntent       RecommendationReason = "ManualIntent"
	RecommendationReasonQueuePressure      RecommendationReason = "QueuePressure"
	RecommendationReasonQueueBelowTarget   RecommendationReason = "QueueBelowTarget"
	RecommendationReasonIdle               RecommendationReason = "Idle"
	RecommendationReasonStable             RecommendationReason = "Stable"
	RecommendationReasonMetricsUnavailable RecommendationReason = "MetricsUnavailable"
	RecommendationReasonMetricsStale       RecommendationReason = "MetricsStale"
	RecommendationReasonMetricsIncomplete  RecommendationReason = "MetricsIncomplete"
)

// ReplicaRecommendation is the Decision stage output before stabilization and lifecycle constraints.
type ReplicaRecommendation struct {
	State    RecommendationState
	Replicas int32
	Reason   RecommendationReason
	Message  string
}

type DecisionAlgorithm interface {
	Name() string
	RecommendReplicas(ScalingSnapshot) (ReplicaRecommendation, error)
}

type DecisionConfig struct {
	TargetAverageQueuedRequests int64
	ScaleUpQueuedRequests       int64
	ScaleDownQueuedRequests     int64
}
