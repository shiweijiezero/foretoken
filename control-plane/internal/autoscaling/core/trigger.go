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
	TriggerReasonPeriodic           TriggerReason = "Periodic"
	TriggerReasonMetricsUnavailable TriggerReason = "MetricsUnavailable"
	TriggerReasonMetricsStale       TriggerReason = "MetricsStale"
	TriggerReasonMetricsIncomplete  TriggerReason = "MetricsIncomplete"
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

// MetricsTriggerDecision converts unavailable, stale, or incomplete metrics into an insufficient-data trigger result.
func MetricsTriggerDecision(snapshot ScalingSnapshot) (TriggerDecision, bool) {
	switch snapshot.Metrics.State {
	case MetricsUnavailable:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerReasonMetricsUnavailable, Message: "scaling metrics are unavailable"}, false
	case MetricsStale:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerReasonMetricsStale, Message: "scaling metrics are stale"}, false
	case MetricsFresh:
		if snapshot.Metrics.Window.Complete {
			return TriggerDecision{}, true
		}
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerReasonMetricsIncomplete, Message: "scaling metrics are incomplete"}, false
	default:
		return TriggerDecision{Disposition: TriggerInsufficientData, Reason: TriggerReasonMetricsUnavailable, Message: "scaling metrics are unavailable"}, false
	}
}
