// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Package algorithm holds the process-wide registry for statically linked autoscaling implementations.
package algorithm

import (
	"fmt"
	"sync"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

type TriggerAlgorithmFactory func() (core.TriggerAlgorithm, error)
type DecisionAlgorithmFactory func(core.DecisionConfig) (core.DecisionAlgorithm, error)
type AdjustmentAlgorithmFactory func(core.AdjustmentConfig) (core.AdjustmentAlgorithm, error)

type registry struct {
	mu          sync.RWMutex
	triggers    map[string]TriggerAlgorithmFactory
	decisions   map[string]DecisionAlgorithmFactory
	adjustments map[string]AdjustmentAlgorithmFactory
}

var builtin = &registry{
	triggers:    make(map[string]TriggerAlgorithmFactory),
	decisions:   make(map[string]DecisionAlgorithmFactory),
	adjustments: make(map[string]AdjustmentAlgorithmFactory),
}

// RegisterTriggerAlgorithm adds a trigger factory to the process-wide builtin registry.
func RegisterTriggerAlgorithm(name string, factory TriggerAlgorithmFactory) error {
	return builtin.register(name, factory != nil, func() bool { _, exists := builtin.triggers[name]; return exists }, func() { builtin.triggers[name] = factory }, "trigger")
}

// RegisterDecisionAlgorithm adds a decision factory to the process-wide builtin registry.
func RegisterDecisionAlgorithm(name string, factory DecisionAlgorithmFactory) error {
	return builtin.register(name, factory != nil, func() bool { _, exists := builtin.decisions[name]; return exists }, func() { builtin.decisions[name] = factory }, "decision")
}

// RegisterAdjustmentAlgorithm adds an adjustment factory to the process-wide builtin registry.
func RegisterAdjustmentAlgorithm(name string, factory AdjustmentAlgorithmFactory) error {
	return builtin.register(name, factory != nil, func() bool { _, exists := builtin.adjustments[name]; return exists }, func() { builtin.adjustments[name] = factory }, "adjustment")
}

func (registry *registry) register(name string, valid bool, exists func() bool, add func(), category string) error {
	if name == "" {
		return fmt.Errorf("autoscaling %s algorithm name must not be empty", category)
	}
	if !valid {
		return fmt.Errorf("autoscaling %s algorithm %q factory is required", category, name)
	}
	registry.mu.Lock()
	defer registry.mu.Unlock()
	if exists() {
		return fmt.Errorf("autoscaling %s algorithm %q is registered twice", category, name)
	}
	add()
	return nil
}

// BuildTrigger constructs a named trigger algorithm from the builtin registry.
func BuildTrigger(name string) (core.TriggerAlgorithm, error) {
	builtin.mu.RLock()
	factory, ok := builtin.triggers[name]
	builtin.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling trigger algorithm %q", name)
	}
	return factory()
}

// BuildDecision constructs a named decision algorithm from the builtin registry.
func BuildDecision(name string, config core.DecisionConfig) (core.DecisionAlgorithm, error) {
	builtin.mu.RLock()
	factory, ok := builtin.decisions[name]
	builtin.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling decision algorithm %q", name)
	}
	return factory(config)
}

// BuildAdjustment constructs a named adjustment algorithm from the builtin registry.
func BuildAdjustment(name string, config core.AdjustmentConfig) (core.AdjustmentAlgorithm, error) {
	builtin.mu.RLock()
	factory, ok := builtin.adjustments[name]
	builtin.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling adjustment algorithm %q", name)
	}
	return factory(config)
}
