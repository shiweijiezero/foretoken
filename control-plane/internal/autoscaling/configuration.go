// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package autoscaling

import "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"

type DecisionAlgorithmName string
type TriggerAlgorithmName string
type AdjustmentAlgorithmName string

const (
	DecisionAlgorithmManual    DecisionAlgorithmName   = "manual"
	DecisionAlgorithmQueue     DecisionAlgorithmName   = "queue"
	DecisionAlgorithmThreshold DecisionAlgorithmName   = "threshold"
	TriggerAlgorithmPeriodic   TriggerAlgorithmName    = "periodic"
	TriggerAlgorithmWatermark  TriggerAlgorithmName    = "watermark"
	AdjustmentAlgorithmDirect  AdjustmentAlgorithmName = "direct"
	AdjustmentAlgorithmStep    AdjustmentAlgorithmName = "step"
)

type Configuration struct {
	DecisionAlgorithm   DecisionAlgorithmName
	TriggerAlgorithm    TriggerAlgorithmName
	AdjustmentAlgorithm AdjustmentAlgorithmName
	Decision            core.DecisionConfig
	Trigger             core.TriggerConfig
}
