// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Package algorithm holds registries for replaceable autoscaling implementations.
package algorithm

import (
	"fmt"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"sort"
	"sync"
)

type TriggerAlgorithmFactory func(core.TriggerConfig) (core.TriggerAlgorithm, error)
type DecisionAlgorithmFactory func(core.DecisionConfig) (core.DecisionAlgorithm, error)
type AdjustmentAlgorithmFactory func() (core.AdjustmentAlgorithm, error)
type Registry struct {
	mu          sync.RWMutex
	triggers    map[string]TriggerAlgorithmFactory
	decisions   map[string]DecisionAlgorithmFactory
	adjustments map[string]AdjustmentAlgorithmFactory
}

// NewRegistry creates an empty concurrent registry for autoscaling algorithm factories.
func NewRegistry() *Registry {
	return &Registry{triggers: map[string]TriggerAlgorithmFactory{}, decisions: map[string]DecisionAlgorithmFactory{}, adjustments: map[string]AdjustmentAlgorithmFactory{}}
}

var builtin = NewRegistry()

// RegisterTriggerAlgorithm adds a trigger factory to the process-wide builtin registry.
func RegisterTriggerAlgorithm(name string, factory TriggerAlgorithmFactory) error {
	return builtin.RegisterTriggerAlgorithm(name, factory)
}

// RegisterDecisionAlgorithm adds a decision factory to the process-wide builtin registry.
func RegisterDecisionAlgorithm(name string, factory DecisionAlgorithmFactory) error {
	return builtin.RegisterDecisionAlgorithm(name, factory)
}

// RegisterAdjustmentAlgorithm adds an adjustment factory to the process-wide builtin registry.
func RegisterAdjustmentAlgorithm(name string, factory AdjustmentAlgorithmFactory) error {
	return builtin.RegisterAdjustmentAlgorithm(name, factory)
}

// RegisterTriggerAlgorithm adds one trigger factory to the registry.
func (registry *Registry) RegisterTriggerAlgorithm(name string, factory TriggerAlgorithmFactory) error {
	return registry.register(name, factory != nil, func() bool { _, exists := registry.triggers[name]; return exists }, func() { registry.triggers[name] = factory }, "trigger")
}

// RegisterDecisionAlgorithm adds one decision factory to the registry.
func (registry *Registry) RegisterDecisionAlgorithm(name string, factory DecisionAlgorithmFactory) error {
	return registry.register(name, factory != nil, func() bool { _, exists := registry.decisions[name]; return exists }, func() { registry.decisions[name] = factory }, "decision")
}

// RegisterAdjustmentAlgorithm adds one adjustment factory to the registry.
func (registry *Registry) RegisterAdjustmentAlgorithm(name string, factory AdjustmentAlgorithmFactory) error {
	return registry.register(name, factory != nil, func() bool { _, exists := registry.adjustments[name]; return exists }, func() { registry.adjustments[name] = factory }, "adjustment")
}

// register validates and stores one algorithm factory under the registry lock.
func (registry *Registry) register(name string, valid bool, exists func() bool, add func(), category string) error {
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

// BuildTrigger constructs the named trigger algorithm from its configuration.
func (registry *Registry) BuildTrigger(name string, config core.TriggerConfig) (core.TriggerAlgorithm, error) {
	registry.mu.RLock()
	factory, ok := registry.triggers[name]
	registry.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling trigger algorithm %q", name)
	}
	return factory(config)
}

// BuildDecision constructs the named decision algorithm from its configuration.
func (registry *Registry) BuildDecision(name string, config core.DecisionConfig) (core.DecisionAlgorithm, error) {
	registry.mu.RLock()
	factory, ok := registry.decisions[name]
	registry.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling decision algorithm %q", name)
	}
	return factory(config)
}

// BuildAdjustment constructs the named adjustment algorithm.
func (registry *Registry) BuildAdjustment(name string) (core.AdjustmentAlgorithm, error) {
	registry.mu.RLock()
	factory, ok := registry.adjustments[name]
	registry.mu.RUnlock()
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling adjustment algorithm %q", name)
	}
	return factory()
}

// Names returns the registered trigger, decision, and adjustment names in sorted order.
func (registry *Registry) Names() (triggers, decisions, adjustments []string) {
	registry.mu.RLock()
	defer registry.mu.RUnlock()
	for name := range registry.triggers {
		triggers = append(triggers, name)
	}
	for name := range registry.decisions {
		decisions = append(decisions, name)
	}
	for name := range registry.adjustments {
		adjustments = append(adjustments, name)
	}
	sort.Strings(triggers)
	sort.Strings(decisions)
	sort.Strings(adjustments)
	return
}

// BuildTrigger constructs a named trigger algorithm from the builtin registry.
func BuildTrigger(name string, config core.TriggerConfig) (core.TriggerAlgorithm, error) {
	return builtin.BuildTrigger(name, config)
}

// BuildDecision constructs a named decision algorithm from the builtin registry.
func BuildDecision(name string, config core.DecisionConfig) (core.DecisionAlgorithm, error) {
	return builtin.BuildDecision(name, config)
}

// BuildAdjustment constructs a named adjustment algorithm from the builtin registry.
func BuildAdjustment(name string) (core.AdjustmentAlgorithm, error) {
	return builtin.BuildAdjustment(name)
}

// Names returns all builtin algorithm names grouped by pipeline stage.
func Names() (triggers, decisions, adjustments []string) { return builtin.Names() }
