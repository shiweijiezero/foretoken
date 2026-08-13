// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Assembles statically linked autoscaling algorithms through their typed registry.
package autoscaling

import (
	"context"
	"errors"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	_ "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/adjustment"
	_ "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/decision"
	_ "github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/trigger"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

var ErrAutoscalerRequired = errors.New("autoscaler is required")

type Autoscaler struct{ pipeline core.Pipeline }

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
	adjustment, err := algorithm.BuildAdjustment(adjustmentName)
	if err != nil {
		return nil, err
	}
	pipeline := core.Pipeline{DecisionAlgorithm: decision, AdjustmentAlgorithm: adjustment, Automatic: automatic}
	if !automatic {
		pipeline.Resolver.AllowDuringTransition = true
	}
	if automatic {
		triggerName := string(configuration.TriggerAlgorithm)
		if triggerName == "" {
			triggerName = string(TriggerAlgorithmPeriodic)
		}
		trigger, err := algorithm.BuildTrigger(triggerName, configuration.Trigger)
		if err != nil {
			return nil, err
		}
		pipeline.TriggerAlgorithm = trigger
	}
	return &Autoscaler{pipeline: pipeline}, nil
}
func Manual() *Autoscaler {
	autoscaler, err := New(Configuration{DecisionAlgorithm: DecisionAlgorithmManual})
	if err != nil {
		panic(err)
	}
	return autoscaler
}
func (autoscaler *Autoscaler) Automatic() bool {
	return autoscaler != nil && autoscaler.pipeline.Automatic
}
func (autoscaler *Autoscaler) TriggerAlgorithmName() string {
	if autoscaler == nil || autoscaler.pipeline.TriggerAlgorithm == nil {
		return ""
	}
	return autoscaler.pipeline.TriggerAlgorithm.Name()
}
func (autoscaler *Autoscaler) AdjustmentAlgorithmName() string {
	if autoscaler == nil || autoscaler.pipeline.AdjustmentAlgorithm == nil {
		return ""
	}
	return autoscaler.pipeline.AdjustmentAlgorithm.Name()
}
func (autoscaler *Autoscaler) Plan(ctx context.Context, snapshots []core.ScalingSnapshot) ([]core.ScalingDecision, error) {
	if autoscaler == nil {
		return nil, ErrAutoscalerRequired
	}
	return autoscaler.pipeline.Plan(ctx, snapshots)
}
func NewWithAlgorithms(decision core.DecisionAlgorithm, trigger core.TriggerAlgorithm, adjustment core.AdjustmentAlgorithm, automatic bool) *Autoscaler {
	return &Autoscaler{pipeline: core.Pipeline{DecisionAlgorithm: decision, TriggerAlgorithm: trigger, AdjustmentAlgorithm: adjustment, Automatic: automatic}}
}
