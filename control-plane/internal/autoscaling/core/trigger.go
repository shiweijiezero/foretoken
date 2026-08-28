// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

type TriggerDisposition string

const (
	TriggerFire             TriggerDisposition = "Fire"
	TriggerHold             TriggerDisposition = "Hold"
	TriggerInsufficientData TriggerDisposition = "InsufficientData"
)

type TriggerReason string

const (
	TriggerReasonPeriodic                              TriggerReason = "Periodic"
	TriggerReasonHighWatermark                         TriggerReason = "HighWatermark"
	TriggerReasonLowWatermark                          TriggerReason = "LowWatermark"
	TriggerReasonWithinWatermarkBand                   TriggerReason = "WithinWatermarkBand"
	TriggerDesiredCapacityReasonObservationUnavailable TriggerReason = "ObservationUnavailable"
	TriggerDesiredCapacityReasonObservationStale       TriggerReason = "ObservationStale"
	TriggerDesiredCapacityReasonObservationIncomplete  TriggerReason = "ObservationIncomplete"
)

type TriggerDecision struct {
	Disposition TriggerDisposition
	Reason      TriggerReason
	Message     string
}
type TriggerAlgorithm interface {
	Name() string
	Decide(ScalingSnapshot) TriggerDecision
}
type TriggerConfig struct {
	LowQueuePerRoutableGroup  int64
	HighQueuePerRoutableGroup int64
}

// ObservationTriggerDecision converts unavailable, stale, or incomplete demand into an insufficient-data trigger result consumed by the scaling pipeline.
func ObservationTriggerDecision(snapshot ScalingSnapshot) (TriggerDecision, bool) {
	switch snapshot.Observation.State {
	case ObservationUnavailable:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerDesiredCapacityReasonObservationUnavailable, Message: "demand observations are unavailable"}, false
	case ObservationStale:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerDesiredCapacityReasonObservationStale, Message: "demand observations are stale"}, false
	case ObservationFresh:
		if snapshot.Observation.Window.Complete {
			return TriggerDecision{}, true
		}
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerDesiredCapacityReasonObservationIncomplete, Message: "demand observation window is incomplete"}, false
	default:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerDesiredCapacityReasonObservationUnavailable, Message: "demand observations are unavailable"}, false
	}
}
