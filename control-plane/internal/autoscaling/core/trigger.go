// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

type TriggerDisposition string

const (
	TriggerFire             TriggerDisposition = "Fire"
	TriggerInsufficientData TriggerDisposition = "InsufficientData"
)

type TriggerReason string

const (
	TriggerReasonPeriodic               TriggerReason = "Periodic"
	TriggerReasonObservationUnavailable TriggerReason = "ObservationUnavailable"
	TriggerReasonObservationStale       TriggerReason = "ObservationStale"
	TriggerReasonObservationIncomplete  TriggerReason = "ObservationIncomplete"
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

// ObservationTriggerDecision converts unavailable, stale, or incomplete demand into an insufficient-data trigger result.
func ObservationTriggerDecision(snapshot ScalingSnapshot) (TriggerDecision, bool) {
	switch snapshot.Observation.State {
	case ObservationUnavailable:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerReasonObservationUnavailable, Message: "demand observations are unavailable"}, false
	case ObservationStale:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerReasonObservationStale, Message: "demand observations are stale"}, false
	case ObservationFresh:
		if snapshot.Observation.Window.Complete {
			return TriggerDecision{}, true
		}
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerReasonObservationIncomplete, Message: "demand observation window is incomplete"}, false
	default:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerReasonObservationUnavailable, Message: "demand observations are unavailable"}, false
	}
}
