// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Statically assembles autoscaling algorithms and evaluates Pool snapshots.

package autoscaling

import (
	"context"
	"fmt"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

// Autoscaler evaluates snapshots with one statically selected algorithm.
type Autoscaler struct {
	evaluator core.Evaluator
}

func New(configuration Configuration) (*Autoscaler, error) {
	name := configuration.Algorithm
	if name == "" {
		name = AlgorithmManual
	}
	switch name {
	case AlgorithmManual:
		return Manual(), nil
	case AlgorithmQueue:
		return NewWithAlgorithm(algorithm.QueueAlgorithm{TargetQueuePerRoutableGroup: configuration.TargetQueuePerRoutableGroup}), nil
	default:
		return nil, fmt.Errorf("unknown autoscaling algorithm %q", name)
	}
}

// Manual returns the production default that preserves ModelService replicas.
func Manual() *Autoscaler {
	return &Autoscaler{evaluator: core.Evaluator{Algorithm: algorithm.ManualAlgorithm{}, AllowDuringTransition: true}}
}

// NewWithAlgorithm supports in-tree community algorithms without dynamic plugins.
func NewWithAlgorithm(selected algorithm.Algorithm) *Autoscaler {
	return &Autoscaler{evaluator: core.Evaluator{Algorithm: selected}}
}

func (autoscaler *Autoscaler) Plan(ctx context.Context, snapshots []algorithm.Snapshot) ([]core.Decision, error) {
	if autoscaler == nil {
		return nil, fmt.Errorf("autoscaler is required")
	}
	decisions := make([]core.Decision, 0, len(snapshots))
	seen := make(map[algorithm.TargetID]struct{}, len(snapshots))
	for _, snapshot := range snapshots {
		if _, exists := seen[snapshot.Target]; exists {
			return nil, fmt.Errorf("duplicate autoscaling target %q", snapshot.Target.Name)
		}
		seen[snapshot.Target] = struct{}{}
		decision, err := autoscaler.evaluator.Evaluate(ctx, snapshot)
		if err != nil {
			return nil, err
		}
		decisions = append(decisions, decision)
	}
	return decisions, nil
}
