// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package autoscaling

// AlgorithmName is the stable name used by Foretoken's static assembly.
type AlgorithmName string

const (
	AlgorithmManual AlgorithmName = "manual"
	AlgorithmQueue  AlgorithmName = "queue"
)

// Configuration contains read-only algorithm selection and configuration.
type Configuration struct {
	Algorithm                   AlgorithmName
	TargetQueuePerRoutableGroup int64
}
