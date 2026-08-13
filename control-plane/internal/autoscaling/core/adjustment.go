// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

type AdjustmentReason string

const (
	AdjustmentReasonDirect   AdjustmentReason = "Direct"
	AdjustmentReasonStepUp   AdjustmentReason = "StepUp"
	AdjustmentReasonStepDown AdjustmentReason = "StepDown"
	AdjustmentReasonHold     AdjustmentReason = "Hold"
)

type AdjustmentInput struct {
	CurrentGroups int32
	DesiredGroups int32
	Bounds        CapacityLimits
}
type ScalingAdjustment struct {
	AdjustedGroups int32
	Reason         AdjustmentReason
	Message        string
}
type AdjustmentAlgorithm interface {
	Name() string
	Adjust(AdjustmentInput) (ScalingAdjustment, error)
}
