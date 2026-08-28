// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package decision

import (
	"context"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Manual struct{}

// Name identifies the manual decision algorithm for registry consumers.
func (Manual) Name() string { return "manual" }

// CalculateDesiredCapacity returns the caller-compiled baseline for manual capacity control.
func (Manual) CalculateDesiredCapacity(_ context.Context, snapshot core.ScalingSnapshot) (core.DesiredCapacity, error) {
	return core.DesiredCapacity{Disposition: core.DesiredCapacityApply, Groups: snapshot.Capacity.BaselineGroups, Reason: core.DesiredCapacityReasonManualIntent, Message: "capacity follows ModelService replicas"}, nil
}
func init() {
	if err := algorithm.RegisterDecisionAlgorithm("manual", func(core.DecisionConfig) (core.DecisionAlgorithm, error) { return Manual{}, nil }); err != nil {
		panic(err)
	}
}
