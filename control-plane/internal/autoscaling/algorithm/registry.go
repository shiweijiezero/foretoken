// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Package algorithm holds the process-wide registry for statically linked autoscaling implementations.
package algorithm

import (
	"encoding/json"
	"fmt"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/adjustment"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/decision"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/trigger"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

// Built-in implementations are declared together and remain fixed for the process lifetime.
var triggers = map[string]func(json.RawMessage) (core.TriggerAlgorithm, error){
	"periodic": trigger.NewPeriodic,
}

var decisions = map[string]func(json.RawMessage) (core.DecisionAlgorithm, error){
	"aimd":            decision.NewAIMD,
	"manual":          decision.NewManual,
	"queue":           decision.NewQueue,
	"queue_threshold": decision.NewQueueThreshold,
}

var adjustments = map[string]func(json.RawMessage, *core.RecommendationHistory) (core.AdjustmentAlgorithm, error){
	"direct": adjustment.NewDirect,
	"step":   adjustment.NewStep,
}

// BuildTrigger constructs a named trigger algorithm from the builtin registry.
func BuildTrigger(name string, parameters json.RawMessage) (core.TriggerAlgorithm, error) {
	factory, ok := triggers[name]
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling trigger algorithm %q", name)
	}
	return factory(parameters)
}

// BuildDecision constructs a named decision algorithm from the builtin registry.
func BuildDecision(name string, config json.RawMessage) (core.DecisionAlgorithm, error) {
	factory, ok := decisions[name]
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling decision algorithm %q", name)
	}
	return factory(config)
}

// BuildAdjustment constructs a named adjustment algorithm from the builtin registry.
func BuildAdjustment(name string, parameters json.RawMessage, history *core.RecommendationHistory) (core.AdjustmentAlgorithm, error) {
	factory, ok := adjustments[name]
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling adjustment algorithm %q", name)
	}
	return factory(parameters, history)
}
