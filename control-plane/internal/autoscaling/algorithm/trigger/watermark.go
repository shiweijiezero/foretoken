// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package trigger

import (
	"fmt"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Watermark struct {
	LowQueuePerRoutableGroup  int64
	HighQueuePerRoutableGroup int64
}

func (Watermark) Name() string { return "watermark" }
func (watermark Watermark) Decide(snapshot core.ScalingSnapshot) core.TriggerDecision {
	if decision, available := core.ObservationTriggerDecision(snapshot); !available {
		return decision
	}
	supply := snapshot.Capacity.RoutableGroups
	if supply < 1 {
		supply = 1
	}
	if exceeds(snapshot.Observation.QueueRequests, watermark.HighQueuePerRoutableGroup, supply) {
		return core.TriggerDecision{Disposition: core.TriggerFire, Reason: core.TriggerReasonHighWatermark, Message: "queue exceeds the high watermark"}
	}
	if atOrBelow(snapshot.Observation.QueueRequests, watermark.LowQueuePerRoutableGroup, supply) && snapshot.Observation.ActiveRequests == 0 {
		return core.TriggerDecision{Disposition: core.TriggerFire, Reason: core.TriggerReasonLowWatermark, Message: "queue is at or below the low watermark with no active requests"}
	}
	return core.TriggerDecision{Disposition: core.TriggerHold, Reason: core.TriggerReasonWithinWatermarkBand, Message: "queue is within the watermark band"}
}
func exceeds(requests, threshold int64, supply int32) bool {
	if requests <= 0 {
		return false
	}
	groups := int64(supply)
	quotient, remainder := requests/groups, requests%groups
	return quotient > threshold || quotient == threshold && remainder > 0
}
func atOrBelow(requests, threshold int64, supply int32) bool {
	if requests < 0 {
		return true
	}
	groups := int64(supply)
	quotient, remainder := requests/groups, requests%groups
	return quotient < threshold || quotient == threshold && remainder == 0
}
func init() {
	if err := algorithm.RegisterTriggerAlgorithm("watermark", func(config core.TriggerConfig) (core.TriggerAlgorithm, error) {
		if config.LowQueuePerRoutableGroup < 0 || config.HighQueuePerRoutableGroup < 0 || config.LowQueuePerRoutableGroup > config.HighQueuePerRoutableGroup {
			return nil, fmt.Errorf("autoscaling trigger watermarks are invalid")
		}
		return Watermark{config.LowQueuePerRoutableGroup, config.HighQueuePerRoutableGroup}, nil
	}); err != nil {
		panic(err)
	}
}
