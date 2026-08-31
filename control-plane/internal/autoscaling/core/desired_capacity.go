// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

type DesiredCapacityDisposition string

const (
	DesiredCapacityApply            DesiredCapacityDisposition = "Apply"
	DesiredCapacityInsufficientData DesiredCapacityDisposition = "InsufficientData"
)

type DesiredCapacityReason string

const (
	DesiredCapacityReasonManualIntent           DesiredCapacityReason = "ManualIntent"
	DesiredCapacityReasonQueuePressure          DesiredCapacityReason = "QueuePressure"
	DesiredCapacityReasonQueueBelowTarget       DesiredCapacityReason = "QueueBelowTarget"
	DesiredCapacityReasonIdle                   DesiredCapacityReason = "Idle"
	DesiredCapacityReasonStable                 DesiredCapacityReason = "Stable"
	DesiredCapacityReasonAtMinimum              DesiredCapacityReason = "AtMinimum"
	DesiredCapacityReasonAtMaximum              DesiredCapacityReason = "AtMaximum"
	DesiredCapacityReasonObservationUnavailable DesiredCapacityReason = "ObservationUnavailable"
	DesiredCapacityReasonObservationStale       DesiredCapacityReason = "ObservationStale"
	DesiredCapacityReasonObservationIncomplete  DesiredCapacityReason = "ObservationIncomplete"
	DesiredCapacityReasonTransitionInProgress   DesiredCapacityReason = "TransitionInProgress"
)

type DesiredCapacity struct {
	Disposition DesiredCapacityDisposition
	Groups      int32
	Reason      DesiredCapacityReason
	Message     string
}

type DecisionAlgorithm interface {
	Name() string
	CalculateDesiredCapacity(ScalingSnapshot) (DesiredCapacity, error)
}

type DecisionConfig struct {
	TargetAverageQueuedRequests int64
	ScaleUpQueuedRequests       int64
	ScaleDownQueuedRequests     int64
}
