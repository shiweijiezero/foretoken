// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Package algorithm constructs statically linked autoscaling implementations.
package algorithm

import (
	"encoding/json"
	"fmt"

	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/adjustment"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/decision"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm/trigger"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
)

// BuildTrigger constructs a named trigger from the stage-owned compiled descriptors.
func BuildTrigger(name string, parameters json.RawMessage) (core.TriggerAlgorithm, error) {
	descriptors, err := triggerRegistry()
	if err != nil {
		return nil, err
	}
	factory, ok := descriptors[name]
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling trigger algorithm %q", name)
	}
	return factory(parameters)
}

// BuildDecision constructs a named decision algorithm from the stage-owned compiled descriptors.
func BuildDecision(name string, parameters json.RawMessage) (core.DecisionAlgorithm, error) {
	descriptors, err := decisionRegistry()
	if err != nil {
		return nil, err
	}
	factory, ok := descriptors[name]
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling decision algorithm %q", name)
	}
	return factory(parameters)
}

// BuildAdjustment constructs a named adjustment from the stage-owned compiled descriptors.
func BuildAdjustment(name string, parameters json.RawMessage, history *core.RecommendationHistory) (core.AdjustmentAlgorithm, error) {
	descriptors, err := adjustmentRegistry()
	if err != nil {
		return nil, err
	}
	factory, ok := descriptors[name]
	if !ok {
		return nil, fmt.Errorf("unknown autoscaling adjustment algorithm %q", name)
	}
	return factory(parameters, history)
}

func triggerRegistry() (map[string]core.TriggerFactory, error) {
	descriptors := trigger.Descriptors()
	registry := make(map[string]core.TriggerFactory, len(descriptors))
	for _, descriptor := range descriptors {
		if err := validateDescriptor("trigger", descriptor.Name); err != nil {
			return nil, err
		}
		if descriptor.Factory == nil {
			return nil, fmt.Errorf("autoscaling trigger algorithm descriptor %q has no factory", descriptor.Name)
		}
		if _, exists := registry[descriptor.Name]; exists {
			return nil, fmt.Errorf("duplicate autoscaling trigger algorithm descriptor %q", descriptor.Name)
		}
		registry[descriptor.Name] = descriptor.Factory
	}
	return registry, nil
}

func decisionRegistry() (map[string]core.DecisionFactory, error) {
	descriptors := decision.Descriptors()
	registry := make(map[string]core.DecisionFactory, len(descriptors))
	for _, descriptor := range descriptors {
		if err := validateDescriptor("decision", descriptor.Name); err != nil {
			return nil, err
		}
		if descriptor.Factory == nil {
			return nil, fmt.Errorf("autoscaling decision algorithm descriptor %q has no factory", descriptor.Name)
		}
		if _, exists := registry[descriptor.Name]; exists {
			return nil, fmt.Errorf("duplicate autoscaling decision algorithm descriptor %q", descriptor.Name)
		}
		registry[descriptor.Name] = descriptor.Factory
	}
	return registry, nil
}

func adjustmentRegistry() (map[string]core.AdjustmentFactory, error) {
	descriptors := adjustment.Descriptors()
	registry := make(map[string]core.AdjustmentFactory, len(descriptors))
	for _, descriptor := range descriptors {
		if err := validateDescriptor("adjustment", descriptor.Name); err != nil {
			return nil, err
		}
		if descriptor.Factory == nil {
			return nil, fmt.Errorf("autoscaling adjustment algorithm descriptor %q has no factory", descriptor.Name)
		}
		if _, exists := registry[descriptor.Name]; exists {
			return nil, fmt.Errorf("duplicate autoscaling adjustment algorithm descriptor %q", descriptor.Name)
		}
		registry[descriptor.Name] = descriptor.Factory
	}
	return registry, nil
}

func validateDescriptor(stage, name string) error {
	if name == "" {
		return fmt.Errorf("autoscaling %s algorithm descriptor has an empty name", stage)
	}
	return nil
}
