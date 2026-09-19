// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Assembles statically linked autoscaling algorithms through their typed registry.
package autoscaling

import (
	"errors"
	"time"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

var ErrAutoscalerRequired = errors.New("autoscaler is required")

type Autoscaler struct{ pipeline core.Pipeline }

// New assembles the configured decision, trigger, and adjustment algorithms into an Autoscaler.
func New(configuration Configuration) (*Autoscaler, error) {
	decisionName := configuration.Decision.Algorithm
	if decisionName == "" {
		decisionName = "manual"
	}
	decision, err := algorithm.BuildDecision(decisionName, configuration.Decision.Parameters)
	if err != nil {
		return nil, err
	}
	adjustmentName := configuration.Adjustment.Algorithm
	if adjustmentName == "" {
		adjustmentName = "step"
	}
	automatic := decisionName != "manual"
	if !automatic {
		adjustmentName = "direct"
	}
	adjustment, err := algorithm.BuildAdjustment(adjustmentName, configuration.Adjustment.Parameters, configuration.History)
	if err != nil {
		return nil, err
	}
	pipeline := core.Pipeline{DecisionAlgorithm: decision, AdjustmentAlgorithm: adjustment, Automatic: automatic}
	if !automatic {
		pipeline.Resolver.AllowDuringTransition = true
	} else {
		triggerName := configuration.Trigger.Algorithm
		if triggerName == "" {
			triggerName = "periodic"
		}
		trigger, err := algorithm.BuildTrigger(triggerName, configuration.Trigger.Parameters)
		if err != nil {
			return nil, err
		}
		pipeline.TriggerAlgorithm = trigger
	}
	return &Autoscaler{pipeline: pipeline}, nil
}

// Manual returns an Autoscaler that applies the ModelService baseline capacity.
func Manual() *Autoscaler {
	autoscaler, err := New(Configuration{Decision: AlgorithmConfiguration{Algorithm: "manual"}})
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

// PollingInterval returns the selected trigger's cadence for controller scheduling; manual control has no polling.
func (autoscaler *Autoscaler) PollingInterval() time.Duration {
	if !autoscaler.Automatic() {
		return 0
	}
	return autoscaler.pipeline.TriggerAlgorithm.PollingInterval()
}
