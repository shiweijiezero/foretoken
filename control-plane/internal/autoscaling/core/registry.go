// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
package core

import "encoding/json"

// TriggerFactory constructs a compiled trigger from user-selected parameters.
type TriggerFactory func(json.RawMessage) (TriggerAlgorithm, error)

// TriggerDescriptor names one compiled trigger and owns its constructor.
type TriggerDescriptor struct {
	Name    string
	Factory TriggerFactory
}

// DecisionFactory constructs a compiled decision algorithm from user-selected parameters.
type DecisionFactory func(json.RawMessage) (DecisionAlgorithm, error)

// DecisionDescriptor names one compiled decision algorithm and owns its constructor.
type DecisionDescriptor struct {
	Name    string
	Factory DecisionFactory
}

// AdjustmentFactory constructs a compiled adjustment algorithm with controller-owned history.
type AdjustmentFactory func(json.RawMessage, *RecommendationHistory) (AdjustmentAlgorithm, error)

// AdjustmentDescriptor names one compiled adjustment algorithm and owns its constructor.
type AdjustmentDescriptor struct {
	Name    string
	Factory AdjustmentFactory
}
