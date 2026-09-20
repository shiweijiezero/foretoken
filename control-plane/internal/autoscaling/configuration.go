// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package autoscaling

import (
	"encoding/json"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

// AlgorithmConfiguration carries the selected stage name and its optional parameter object.
type AlgorithmConfiguration struct {
	Algorithm  string
	Parameters json.RawMessage
}

// Configuration combines stage choices with controller-owned recommendation history.
type Configuration struct {
	Decision   AlgorithmConfiguration
	Trigger    AlgorithmConfiguration
	Adjustment AlgorithmConfiguration
	History    *core.RecommendationHistory
}
