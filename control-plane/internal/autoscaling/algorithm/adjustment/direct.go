// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package adjustment

import (
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type Direct struct{}

// Name identifies the direct adjustment algorithm for registry consumers.
func (Direct) Name() string { return "direct" }

// Adjust clamps the desired capacity to the configured hard bounds.
func (Direct) Adjust(input core.AdjustmentInput) (core.ScalingAdjustment, error) {
	return core.ScalingAdjustment{AdjustedGroups: clip(input.DesiredGroups, input.Bounds.MinGroups, input.Bounds.MaxGroups), Reason: core.AdjustmentReasonDirect, Message: "desired capacity is applied directly"}, nil
}
func init() {
	if err := algorithm.RegisterAdjustmentAlgorithm("direct", func() (core.AdjustmentAlgorithm, error) { return Direct{}, nil }); err != nil {
		panic(err)
	}
}
