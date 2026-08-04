// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Compiles ModelService intent into deterministic ModelPool templates.

package compiler

import (
	"fmt"
	"sort"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/resource"
)

const defaultPoolName = "default"

// ModelPool is one normalized Pool produced from ModelService intent.
type ModelPool struct {
	Name          string
	DesiredGroups int32
	Template      inferencev1alpha1.NormalizedPoolTemplate
}

// CompileModelService normalizes shorthand or advanced Pool intent without resolving platform runtime defaults.
func CompileModelService(spec inferencev1alpha1.ModelServiceSpec) ([]ModelPool, error) {
	timeouts, err := normalizeTimeouts(spec.Timeouts)
	if err != nil {
		return nil, err
	}

	if len(spec.ModelPools) == 0 {
		replicas := valueOrDefault(spec.Replicas, 1)
		nodes := valueOrDefault(spec.Nodes, 1)
		pool, err := compilePool(spec, defaultPoolName, inferencev1alpha1.ModelRoleAggregate, replicas, nodes, "", *spec.Resources, *spec.Parallelism, timeouts)
		if err != nil {
			return nil, err
		}
		return []ModelPool{pool}, nil
	}

	pools := make([]ModelPool, 0, len(spec.ModelPools))
	for _, entry := range spec.ModelPools {
		role := entry.Role
		if role == "" {
			role = inferencev1alpha1.ModelRoleAggregate
		}
		replicas := valueOrDefault(entry.Replicas, 1)
		nodes := valueOrDefault(entry.Nodes, 1)
		pool, err := compilePool(spec, entry.Name, role, replicas, nodes, entry.Network, entry.Resources, entry.Parallelism, timeouts)
		if err != nil {
			return nil, fmt.Errorf("modelPools %q: %w", entry.Name, err)
		}
		pools = append(pools, pool)
	}

	sort.Slice(pools, func(i, j int) bool { return pools[i].Name < pools[j].Name })
	return pools, nil
}

func compilePool(spec inferencev1alpha1.ModelServiceSpec, name string, role inferencev1alpha1.ModelRole, replicas, nodes int32, network string, resources inferencev1alpha1.ModelResources, parallelism inferencev1alpha1.Parallelism, timeouts inferencev1alpha1.ModelTimeouts) (ModelPool, error) {
	normalizedResources, err := normalizeResources(resources)
	if err != nil {
		return ModelPool{}, err
	}
	compiledParallelism := compileParallelism(parallelism)

	capacity := int64(nodes) * int64(normalizedResources.Requests.GPU.Count)
	ranks := int64(compiledParallelism.PP) * int64(compiledParallelism.TP) * int64(compiledParallelism.PCP) * int64(compiledParallelism.DP)
	if capacity != ranks {
		return ModelPool{}, fmt.Errorf("nodes * resources.requests.gpu.count must equal the compiled worker rank count")
	}

	return ModelPool{
		Name:          name,
		DesiredGroups: replicas,
		Template: inferencev1alpha1.NormalizedPoolTemplate{
			Model:       spec.Model,
			Backend:     spec.Backend,
			Role:        role,
			NodeCount:   nodes,
			MemberCount: nodes,
			Resources:   normalizedResources,
			Parallelism: compiledParallelism,
			Network:     network,
			Timeouts:    timeouts,
			ExtraArgs:   append([]inferencev1alpha1.BackendArg(nil), spec.ExtraArgs...),
		},
	}, nil
}

func compileParallelism(input inferencev1alpha1.Parallelism) inferencev1alpha1.CompiledParallelism {
	tp := defaultOne(input.TP)
	pcp := defaultOne(input.PCP)
	dp := int32(1)
	var ep *inferencev1alpha1.ExpertParallelism
	if input.EP != nil {
		dp = int32(int64(input.EP.Size) / (int64(tp) * int64(pcp)))
		copied := *input.EP
		ep = &copied
	} else if input.DP != nil {
		dp = *input.DP
	}

	return inferencev1alpha1.CompiledParallelism{
		TP:  tp,
		PP:  defaultOne(input.PP),
		DP:  dp,
		PCP: pcp,
		DCP: defaultOne(input.DCP),
		EP:  ep,
	}
}

func normalizeResources(input inferencev1alpha1.ModelResources) (inferencev1alpha1.ModelResources, error) {
	cpu, err := normalizeQuantity("resources.requests.cpu", input.Requests.CPU)
	if err != nil {
		return inferencev1alpha1.ModelResources{}, err
	}
	memory, err := normalizeQuantity("resources.requests.memory", input.Requests.Memory)
	if err != nil {
		return inferencev1alpha1.ModelResources{}, err
	}
	gpu := input.Requests.GPU
	if gpu.Type == "" {
		gpu.Type = "auto"
	}
	if gpu.Count == 0 {
		gpu.Count = 1
	}

	output := inferencev1alpha1.ModelResources{
		Requests: inferencev1alpha1.ModelResourceRequests{
			ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: cpu, Memory: memory},
			GPU:                     gpu,
		},
	}
	if input.Limits != nil {
		limits := new(inferencev1alpha1.ComputeResourceLimits)
		if input.Limits.CPU != nil {
			quantity, err := normalizeQuantity("resources.limits.cpu", *input.Limits.CPU)
			if err != nil {
				return inferencev1alpha1.ModelResources{}, err
			}
			limits.CPU = &quantity
		}
		if input.Limits.Memory != nil {
			quantity, err := normalizeQuantity("resources.limits.memory", *input.Limits.Memory)
			if err != nil {
				return inferencev1alpha1.ModelResources{}, err
			}
			limits.Memory = &quantity
		}
		output.Limits = limits
	}
	return output, nil
}

func normalizeQuantity(field string, input inferencev1alpha1.ResourceQuantity) (inferencev1alpha1.ResourceQuantity, error) {
	quantity, err := resource.ParseQuantity(string(input))
	if err != nil {
		return "", fmt.Errorf("%s must be a valid Kubernetes resource quantity", field)
	}
	return inferencev1alpha1.ResourceQuantity(quantity.String()), nil
}

func normalizeTimeouts(input inferencev1alpha1.ModelTimeouts) (inferencev1alpha1.ModelTimeouts, error) {
	startup, err := normalizeDuration("timeouts.startup", input.Startup)
	if err != nil {
		return inferencev1alpha1.ModelTimeouts{}, err
	}
	drain, err := normalizeDuration("timeouts.drain", input.Drain)
	if err != nil {
		return inferencev1alpha1.ModelTimeouts{}, err
	}
	return inferencev1alpha1.ModelTimeouts{Startup: startup, Drain: drain}, nil
}

func normalizeDuration(field string, input inferencev1alpha1.Duration) (inferencev1alpha1.Duration, error) {
	duration, err := time.ParseDuration(string(input))
	if err != nil || duration <= 0 {
		return "", fmt.Errorf("%s must be a positive duration", field)
	}
	return inferencev1alpha1.Duration(duration.String()), nil
}

func valueOrDefault(value *int32, fallback int32) int32 {
	if value == nil {
		return fallback
	}
	return *value
}

func defaultOne(value int32) int32 {
	if value == 0 {
		return 1
	}
	return value
}
