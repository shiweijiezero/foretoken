// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package trigger

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Periodic struct{ interval time.Duration }

const periodicName = "periodic"

// Name identifies the periodic trigger algorithm for registry consumers.
func (Periodic) Name() string { return periodicName }

var periodicDescriptor = core.TriggerDescriptor{Name: periodicName, Factory: NewPeriodic}

// Decide evaluates every complete fresh metrics snapshot supplied by the controller polling loop.
func (Periodic) Decide(snapshot core.ScalingSnapshot) core.TriggerDecision {
	if decision, available := core.MetricsTriggerDecision(snapshot); !available {
		return decision
	}
	return core.TriggerDecision{Disposition: core.TriggerFire, Reason: core.TriggerReasonPeriodic, Message: "periodic evaluation"}
}

// PollingInterval tells the controller when to schedule the next periodic evaluation.
func (periodic Periodic) PollingInterval() time.Duration { return periodic.interval }

// NewPeriodic decodes the polling interval for the registry; the controller owns scheduling.
func NewPeriodic(parameters json.RawMessage) (core.TriggerAlgorithm, error) {
	interval := "5s"
	if err := core.DecodeParameters(parameters, map[string]any{"interval": &interval}); err != nil {
		return nil, err
	}
	parsed, err := time.ParseDuration(interval)
	if err != nil || parsed <= 0 {
		return nil, fmt.Errorf("autoscaling periodic interval must be a positive duration")
	}
	return Periodic{interval: parsed}, nil
}
