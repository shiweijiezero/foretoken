// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package trigger

import (
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Periodic struct{}

// Name identifies the periodic trigger algorithm for registry consumers.
func (Periodic) Name() string { return "periodic" }

// Decide evaluates every complete fresh metrics snapshot supplied by the controller polling loop.
func (Periodic) Decide(snapshot core.ScalingSnapshot) core.TriggerDecision {
	if decision, available := core.MetricsTriggerDecision(snapshot); !available {
		return decision
	}
	return core.TriggerDecision{Disposition: core.TriggerFire, Reason: core.TriggerReasonPeriodic, Message: "periodic evaluation"}
}

func init() {
	if err := algorithm.RegisterTriggerAlgorithm("periodic", func() (core.TriggerAlgorithm, error) { return Periodic{}, nil }); err != nil {
		panic(err)
	}
}
