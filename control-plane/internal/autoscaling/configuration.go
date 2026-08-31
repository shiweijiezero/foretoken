// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package autoscaling

import "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"

type DecisionAlgorithmName string
type TriggerAlgorithmName string
type AdjustmentAlgorithmName string

const (
	DecisionAlgorithmManual         DecisionAlgorithmName   = "manual"
	DecisionAlgorithmQueue          DecisionAlgorithmName   = "queue"
	DecisionAlgorithmQueueThreshold DecisionAlgorithmName   = "queue_threshold"
	TriggerAlgorithmPeriodic        TriggerAlgorithmName    = "periodic"
	AdjustmentAlgorithmDirect       AdjustmentAlgorithmName = "direct"
	AdjustmentAlgorithmStep         AdjustmentAlgorithmName = "step"
)

type Configuration struct {
	DecisionAlgorithm   DecisionAlgorithmName
	TriggerAlgorithm    TriggerAlgorithmName
	AdjustmentAlgorithm AdjustmentAlgorithmName
	Decision            core.DecisionConfig
	Adjustment          core.AdjustmentConfig
}
