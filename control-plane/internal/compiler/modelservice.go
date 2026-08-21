// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compiles ModelService intent into deterministic ModelPool templates.

package compiler

import (
	"fmt"
	"sort"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/resource"
)

const (
	defaultPoolName         = "default"
	defaultArtifactRevision = "main"
)

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
	if err := validateAutoscalingConfig(spec.Autoscaling); err != nil {
		return nil, err
	}

	internalGenerateRequestBodyLimitBytes := valueOrDefaultInt64(spec.InternalGenerateRequestBodyLimitBytes, inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes)
	if internalGenerateRequestBodyLimitBytes < inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes || internalGenerateRequestBodyLimitBytes > inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes {
		return nil, fmt.Errorf("internalGenerateRequestBodyLimitBytes must be between %d and %d", inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes, inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes)
	}
	if len(spec.ModelPools) == 0 {
		replicas := valueOrDefault(spec.Replicas, 1)
		nodes := valueOrDefault(spec.Nodes, 1)
		pool, err := compilePool(spec, defaultPoolName, inferencev1alpha1.ModelRoleAggregate, replicas, nodes, "", "", *spec.Resources, *spec.Parallelism, spec.MaxInputTokens, internalGenerateRequestBodyLimitBytes, spec.KVCache, spec.Features, timeouts)
		if err != nil {
			return nil, err
		}
		return []ModelPool{pool}, nil
	}
	if err := validateModelPoolRoles(spec.ModelPools); err != nil {
		return nil, err
	}
	if hasEncoderRole(spec.ModelPools) && (spec.ECProfile == nil || spec.ECProfile.Profile == "") {
		return nil, fmt.Errorf("E/P/D modelPools require an EC profile")
	}
	if !hasEncoderRole(spec.ModelPools) && spec.ECProfile != nil {
		return nil, fmt.Errorf("EC profile requires E/P/D modelPools")
	}

	pools := make([]ModelPool, 0, len(spec.ModelPools))
	for _, entry := range spec.ModelPools {
		role := entry.Role
		if role == "" {
			role = inferencev1alpha1.ModelRoleAggregate
		}
		replicas := valueOrDefault(entry.Replicas, 1)
		nodes := valueOrDefault(entry.Nodes, 1)
		pool, err := compilePool(spec, entry.Name, role, replicas, nodes, entry.Network, ecProfileForRole(spec.ECProfile, role), entry.Resources, entry.Parallelism, entry.MaxInputTokens, internalGenerateRequestBodyLimitBytes, entry.KVCache, entry.Features, timeouts)
		if err != nil {
			return nil, fmt.Errorf("modelPools %q: %w", entry.Name, err)
		}
		pools = append(pools, pool)
	}

	sort.Slice(pools, func(i, j int) bool { return pools[i].Name < pools[j].Name })
	return pools, nil
}

// Validate service-wide topology across Pools: aggregate and split roles are exclusive,
// P/D must be paired, and E/P/D requires one equally sized Pool for every stage.
func validateModelPoolRoles(pools []inferencev1alpha1.ModelPoolTemplate) error {
	var aggregate bool
	roleCounts := make(map[inferencev1alpha1.ModelRole]int, 3)
	roleReplicas := make(map[inferencev1alpha1.ModelRole]int32, 3)
	for _, pool := range pools {
		switch pool.Role {
		case "", inferencev1alpha1.ModelRoleAggregate:
			aggregate = true
		case inferencev1alpha1.ModelRoleEncoder, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode:
			roleCounts[pool.Role]++
			roleReplicas[pool.Role] += valueOrDefault(pool.Replicas, 1)
		}
	}
	hasEncoder := roleCounts[inferencev1alpha1.ModelRoleEncoder] > 0
	hasPrefill := roleCounts[inferencev1alpha1.ModelRolePrefill] > 0
	hasDecode := roleCounts[inferencev1alpha1.ModelRoleDecode] > 0
	if aggregate && (hasEncoder || hasPrefill || hasDecode) {
		return fmt.Errorf("modelPools cannot mix aggregate and split roles")
	}
	if hasEncoder {
		if !hasPrefill || !hasDecode {
			return fmt.Errorf("E/P/D modelPools must contain encoder, prefill, and decode roles")
		}
		for _, role := range []inferencev1alpha1.ModelRole{inferencev1alpha1.ModelRoleEncoder, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode} {
			if roleCounts[role] != 1 {
				return fmt.Errorf("E/P/D modelPools must contain exactly one %s Pool", role)
			}
		}
		encoderReplicas := roleReplicas[inferencev1alpha1.ModelRoleEncoder]
		if encoderReplicas != roleReplicas[inferencev1alpha1.ModelRolePrefill] || encoderReplicas != roleReplicas[inferencev1alpha1.ModelRoleDecode] {
			return fmt.Errorf("E/P/D modelPools must have equal encoder, prefill, and decode replica counts")
		}
		return nil
	}
	if hasPrefill != hasDecode {
		return fmt.Errorf("modelPools must contain both prefill and decode roles")
	}
	return nil
}

func compilePool(spec inferencev1alpha1.ModelServiceSpec, name string, role inferencev1alpha1.ModelRole, replicas, nodes int32, network, ecProfile string, resources inferencev1alpha1.ModelResources, parallelism inferencev1alpha1.Parallelism, maxInputTokens *int32, internalGenerateRequestBodyLimitBytes int64, kvCache *inferencev1alpha1.KVCache, features *inferencev1alpha1.ModelFeatures, timeouts inferencev1alpha1.ModelTimeouts) (ModelPool, error) {
	if nodes != 1 {
		return ModelPool{}, fmt.Errorf("only single-node model groups are currently supported")
	}
	normalizedResources, err := normalizeResources(resources)
	if err != nil {
		return ModelPool{}, err
	}
	compiledParallelism := compileParallelism(parallelism)
	if role != inferencev1alpha1.ModelRoleAggregate && (compiledParallelism.TP != 1 || compiledParallelism.PP != 1 || compiledParallelism.DP != 1 || compiledParallelism.PCP != 1 || compiledParallelism.DCP != 1 || compiledParallelism.EP != nil) {
		return ModelPool{}, fmt.Errorf("split serving currently requires TP=PP=DP=PCP=DCP=1 without expert parallelism")
	}
	normalizedKVCache, err := normalizeKVCache(kvCache)
	if err != nil {
		return ModelPool{}, err
	}

	capacity := int64(nodes) * int64(normalizedResources.Requests.GPU.Count)
	ranks := int64(compiledParallelism.PP) * int64(compiledParallelism.TP) * int64(compiledParallelism.PCP) * int64(compiledParallelism.DP)
	if capacity != ranks {
		return ModelPool{}, fmt.Errorf("nodes * resources.requests.gpu.count must equal the compiled worker rank count")
	}
	normalizedFeatures, err := normalizeModelFeatures(features)
	if err != nil {
		return ModelPool{}, err
	}
	tokenizer := spec.Tokenizer
	if tokenizer == "" {
		tokenizer = spec.Model
	}
	return ModelPool{
		Name:          name,
		DesiredGroups: replicas,
		Template: inferencev1alpha1.NormalizedPoolTemplate{
			Model:                                 spec.Model,
			ModelRevision:                         defaultArtifactRevision,
			Tokenizer:                             tokenizer,
			TokenizerRevision:                     defaultArtifactRevision,
			Backend:                               spec.Backend,
			Role:                                  role,
			NodeCount:                             nodes,
			MemberCount:                           nodes,
			Resources:                             normalizedResources,
			Parallelism:                           compiledParallelism,
			MaxInputTokens:                        copyInt32(maxInputTokens),
			InternalGenerateRequestBodyLimitBytes: internalGenerateRequestBodyLimitBytes,
			Network:                               network,
			ECProfile:                             ecProfile,
			Timeouts:                              timeouts,
			KVCache:                               normalizedKVCache,
			Features:                              normalizedFeatures,
			ExtraArgs:                             append([]inferencev1alpha1.BackendArg(nil), spec.ExtraArgs...),
		},
	}, nil
}

// The remaining compiler helpers normalize user shorthand into stable Pool template fields.
// Runtime-incompatible resources, storage, timeouts, and features fail before reconciliation.
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

func normalizeKVCache(input *inferencev1alpha1.KVCache) (*inferencev1alpha1.NormalizedKVCache, error) {
	if input == nil {
		return nil, nil
	}
	if (input.Offload == nil) == (input.MooncakeStore == nil) {
		return nil, fmt.Errorf("kvCache must select exactly one of offload or mooncakeStore")
	}
	output := inferencev1alpha1.NormalizedKVCache{}
	if input.Offload != nil {
		cpu, err := normalizeQuantity("kvCache.offload.cpuCacheSize", input.Offload.CPUCacheSize)
		if err != nil {
			return nil, err
		}
		quantity, _ := resource.ParseQuantity(string(cpu))
		bytes, exact := quantity.AsInt64()
		if !exact || bytes <= 0 {
			return nil, fmt.Errorf("kvCache.offload.cpuCacheSize must be a positive integer number of bytes")
		}
		offload := *input.Offload
		offload.CPUCacheSize = cpu
		output.Offload = &offload
	}
	if input.MooncakeStore != nil {
		store := *input.MooncakeStore
		if (store.Profile == "") == (store.KVServiceRef == nil) {
			return nil, fmt.Errorf("kvCache.mooncakeStore must select exactly one of profile or kvServiceRef")
		}
		if store.KVServiceRef != nil && store.KVServiceRef.Name == "" {
			return nil, fmt.Errorf("kvCache.mooncakeStore.kvServiceRef.name is required")
		}
		output.MooncakeStore = &inferencev1alpha1.NormalizedMooncakeStore{Profile: store.Profile}
	}
	return &output, nil
}

func validateAutoscalingConfig(config *inferencev1alpha1.ModelAutoscalingConfig) error {
	if config == nil {
		return nil
	}
	minimum := int32(0)
	if config.MinGroups != nil {
		minimum = *config.MinGroups
	}
	if minimum < 0 || config.MaxGroups != nil && *config.MaxGroups < minimum {
		return fmt.Errorf("autoscaling group bounds are invalid")
	}
	if config.Algorithm != "" && config.Algorithm != inferencev1alpha1.AutoscalingAlgorithmManual && config.MaxGroups == nil {
		return fmt.Errorf("autoscaling maxGroups is required unless algorithm is manual")
	}
	if config.TargetQueuePerRoutableGroup != nil && *config.TargetQueuePerRoutableGroup < 0 || config.ScaleUpQueue != nil && *config.ScaleUpQueue < 0 {
		return fmt.Errorf("autoscaling thresholds must be non-negative")
	}
	if adjustment := config.Adjustment; adjustment != nil {
		if adjustment.MaxScaleUpGroups != nil && *adjustment.MaxScaleUpGroups < 0 || adjustment.MaxScaleDownGroups != nil && *adjustment.MaxScaleDownGroups < 0 {
			return fmt.Errorf("autoscaling adjustment step limits must be non-negative")
		}
	}
	if trigger := config.Trigger; trigger != nil {
		low := valueOrDefaultInt64(trigger.LowQueuePerRoutableGroup, 0)
		high := valueOrDefaultInt64(trigger.HighQueuePerRoutableGroup, 1)
		if low < 0 || high < 0 || low > high {
			return fmt.Errorf("autoscaling trigger watermarks are invalid")
		}
	}
	for field, value := range map[string]inferencev1alpha1.Duration{
		"autoscaling.trigger.interval": triggerIntervalDuration(config.Trigger),
		"autoscaling.metricsMaxAge":    valueOrDefaultDuration(config.MetricsMaxAge, "15s"),
	} {
		if duration, err := time.ParseDuration(string(value)); err != nil || duration <= 0 {
			return fmt.Errorf("%s must be a positive duration", field)
		}
	}
	return nil
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

func valueOrDefaultDuration(value inferencev1alpha1.Duration, fallback inferencev1alpha1.Duration) inferencev1alpha1.Duration {
	if value == "" {
		return fallback
	}
	return value
}

func triggerIntervalDuration(trigger *inferencev1alpha1.ModelAutoscalingTriggerConfig) inferencev1alpha1.Duration {
	if trigger == nil {
		return "5s"
	}
	return valueOrDefaultDuration(trigger.Interval, "5s")
}

func valueOrDefaultInt64(value *int64, fallback int64) int64 {
	if value == nil {
		return fallback
	}
	return *value
}

func normalizeModelFeatures(input *inferencev1alpha1.ModelFeatures) (inferencev1alpha1.ModelFeatures, error) {
	if input == nil {
		return inferencev1alpha1.ModelFeatures{}, nil
	}
	output := inferencev1alpha1.ModelFeatures{Tools: input.Tools, Reasoning: input.Reasoning}
	structured := make(map[inferencev1alpha1.StructuredOutputFormat]struct{}, len(input.StructuredOutputs))
	for _, format := range input.StructuredOutputs {
		if format != inferencev1alpha1.StructuredOutputFormatJSONObject && format != inferencev1alpha1.StructuredOutputFormatJSONSchema {
			return inferencev1alpha1.ModelFeatures{}, fmt.Errorf("unsupported structured output format %q", format)
		}
		structured[format] = struct{}{}
	}
	for format := range structured {
		output.StructuredOutputs = append(output.StructuredOutputs, format)
	}
	sort.Slice(output.StructuredOutputs, func(i, j int) bool { return output.StructuredOutputs[i] < output.StructuredOutputs[j] })
	modalities := make(map[inferencev1alpha1.MultimodalModality]struct{}, len(input.Multimodal))
	for _, modality := range input.Multimodal {
		if modality != inferencev1alpha1.MultimodalModalityImage {
			return inferencev1alpha1.ModelFeatures{}, fmt.Errorf("unsupported multimodal modality %q", modality)
		}
		modalities[modality] = struct{}{}
	}
	for modality := range modalities {
		output.Multimodal = append(output.Multimodal, modality)
	}
	sort.Slice(output.Multimodal, func(i, j int) bool { return output.Multimodal[i] < output.Multimodal[j] })
	return output, nil
}

func copyInt32(value *int32) *int32 {
	if value == nil {
		return nil
	}
	copied := *value
	return &copied
}

func defaultOne(value int32) int32 {
	if value == 0 {
		return 1
	}
	return value
}

func ecProfileForRole(profile *inferencev1alpha1.ECProfileReference, role inferencev1alpha1.ModelRole) string {
	if profile == nil || (role != inferencev1alpha1.ModelRoleEncoder && role != inferencev1alpha1.ModelRolePrefill) {
		return ""
	}
	return profile.Profile
}

func hasEncoderRole(pools []inferencev1alpha1.ModelPoolTemplate) bool {
	for _, pool := range pools {
		if pool.Role == inferencev1alpha1.ModelRoleEncoder {
			return true
		}
	}
	return false
}
