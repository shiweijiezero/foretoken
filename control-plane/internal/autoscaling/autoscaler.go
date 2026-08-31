// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Assembles statically linked autoscaling algorithms through their typed registry.
package autoscaling

import (
	"errors"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	_ "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/adjustment"
	_ "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/decision"
	_ "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/trigger"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

var ErrAutoscalerRequired = errors.New("autoscaler is required")

type Autoscaler struct{ pipeline core.Pipeline }

// New assembles the configured decision, trigger, and adjustment algorithms into an Autoscaler.
func New(configuration Configuration) (*Autoscaler, error) {
	decisionName := string(configuration.DecisionAlgorithm)
	if decisionName == "" {
		decisionName = string(DecisionAlgorithmManual)
	}
	decision, err := algorithm.BuildDecision(decisionName, configuration.Decision)
	if err != nil {
		return nil, err
	}
	adjustmentName := string(configuration.AdjustmentAlgorithm)
	if adjustmentName == "" {
		adjustmentName = string(AdjustmentAlgorithmStep)
	}
	automatic := decisionName != string(DecisionAlgorithmManual)
	if !automatic {
		adjustmentName = string(AdjustmentAlgorithmDirect)
	}
	adjustment, err := algorithm.BuildAdjustment(adjustmentName, configuration.Adjustment)
	if err != nil {
		return nil, err
	}
	pipeline := core.Pipeline{DecisionAlgorithm: decision, AdjustmentAlgorithm: adjustment, Automatic: automatic}
	if !automatic {
		pipeline.Resolver.AllowDuringTransition = true
	} else {
		triggerName := string(configuration.TriggerAlgorithm)
		if triggerName == "" {
			triggerName = string(TriggerAlgorithmPeriodic)
		}
		trigger, err := algorithm.BuildTrigger(triggerName)
		if err != nil {
			return nil, err
		}
		pipeline.TriggerAlgorithm = trigger
	}
	return &Autoscaler{pipeline: pipeline}, nil
}

// Manual returns an Autoscaler that applies the ModelService baseline capacity.
func Manual() *Autoscaler {
	autoscaler, err := New(Configuration{DecisionAlgorithm: DecisionAlgorithmManual})
	if err != nil {
		panic(err)
	}
	return autoscaler
}

// Automatic reports whether the Autoscaler evaluates demand-driven capacity.
func (autoscaler *Autoscaler) Automatic() bool {
	return autoscaler != nil && autoscaler.pipeline.Automatic
}

// TriggerAlgorithmName reports the configured trigger algorithm for status consumers.
func (autoscaler *Autoscaler) TriggerAlgorithmName() string {
	if autoscaler == nil || autoscaler.pipeline.TriggerAlgorithm == nil {
		return ""
	}
	return autoscaler.pipeline.TriggerAlgorithm.Name()
}

// Plan evaluates scaling snapshots through the configured autoscaling pipeline.
func (autoscaler *Autoscaler) Plan(snapshots []core.ScalingSnapshot) ([]core.ScalingDecision, error) {
	if autoscaler == nil {
		return nil, ErrAutoscalerRequired
	}
	return autoscaler.pipeline.Plan(snapshots)
}
