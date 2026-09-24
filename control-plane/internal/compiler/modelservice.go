// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compiles ModelService intent into deterministic ModelPool templates.

package compiler

import (
	"fmt"
	"path"
	"sort"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/resource"
)

const (
	defaultPoolName           = "default"
	defaultHFRevision         = "main"
	defaultModelScopeRevision = "master"
	localArtifactRevision     = "local"
)

// ModelPool is one normalized Pool produced from ModelService intent.
type ModelPool struct {
	Name          string
	DesiredGroups int32
	Template      inferencev1alpha1.NormalizedPoolTemplate
}

// CompileModelService normalizes shorthand or advanced Pool intent without resolving platform access settings.
func CompileModelService(spec inferencev1alpha1.ModelServiceSpec) ([]ModelPool, error) {
	source := spec.Source
	if source == "" {
		source = inferencev1alpha1.ModelSourceHF
	}
	artifactRevision := defaultHFRevision
	switch source {
	case inferencev1alpha1.ModelSourceLocal:
		artifactRevision = localArtifactRevision
	case inferencev1alpha1.ModelSourceHF:
	case inferencev1alpha1.ModelSourceModelScope:
		artifactRevision = defaultModelScopeRevision
	default:
		return nil, fmt.Errorf("source must be local, hf, or modelscope")
	}
	if source != inferencev1alpha1.ModelSourceLocal && (path.IsAbs(spec.Model) || path.IsAbs(spec.Tokenizer)) {
		return nil, fmt.Errorf("absolute model and tokenizer paths require source local")
	}
	timeouts, err := normalizeTimeouts(spec.Timeouts)
	if err != nil {
		return nil, err
	}
	internalGenerateRequestBodyLimitBytes := valueOrDefaultInt64(spec.InternalGenerateRequestBodyLimitBytes, inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes)
	if internalGenerateRequestBodyLimitBytes < inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes || internalGenerateRequestBodyLimitBytes > inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes {
		return nil, fmt.Errorf("internalGenerateRequestBodyLimitBytes must be between %d and %d", inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes, inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes)
	}
	if len(spec.ModelPools) == 0 {
		replicas := valueOrDefault(spec.Replicas, 1)
		nodes := valueOrDefault(spec.Nodes, 1)
		pool, err := compilePool(spec, source, artifactRevision, defaultPoolName, inferencev1alpha1.ModelRoleAggregate, replicas, nodes, "", "", *spec.Resources, spec.EngineArgs, spec.MaxInputTokens, internalGenerateRequestBodyLimitBytes, spec.KVCache, spec.Features, timeouts)
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
		engineArgs := spec.EngineArgs
		if entry.EngineArgs != nil {
			engineArgs = *entry.EngineArgs
		}
		pool, err := compilePool(spec, source, artifactRevision, entry.Name, role, replicas, nodes, entry.Network, ecProfileForRole(spec.ECProfile, role), entry.Resources, engineArgs, entry.MaxInputTokens, internalGenerateRequestBodyLimitBytes, entry.KVCache, entry.Features, timeouts)
		if err != nil {
			return nil, fmt.Errorf("modelPools %q: %w", entry.Name, err)
		}
		pools = append(pools, pool)
	}

	sort.Slice(pools, func(i, j int) bool { return pools[i].Name < pools[j].Name })
	return pools, nil
}

// Validate service-wide topology across Pools: aggregate and split roles are exclusive,
// and split topologies contain the stages required by their role.
func validateModelPoolRoles(pools []inferencev1alpha1.ModelPoolTemplate) error {
	var aggregate bool
	roleCounts := make(map[inferencev1alpha1.ModelRole]int, 3)
	for _, pool := range pools {
		switch pool.Role {
		case "", inferencev1alpha1.ModelRoleAggregate:
			aggregate = true
		case inferencev1alpha1.ModelRoleEncoder, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode:
			roleCounts[pool.Role]++
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
		return nil
	}
	if hasPrefill != hasDecode {
		return fmt.Errorf("modelPools must contain both prefill and decode roles")
	}
	return nil
}

func compilePool(spec inferencev1alpha1.ModelServiceSpec, source inferencev1alpha1.ModelSource, artifactRevision, name string, role inferencev1alpha1.ModelRole, replicas, nodes int32, network, ecProfile string, resources inferencev1alpha1.ModelResources, engineArgs inferencev1alpha1.EngineArguments, maxInputTokens *int32, internalGenerateRequestBodyLimitBytes int64, kvCache *inferencev1alpha1.KVCache, features *inferencev1alpha1.ModelFeatures, timeouts inferencev1alpha1.ModelTimeouts) (ModelPool, error) {
	if nodes < 1 {
		return ModelPool{}, fmt.Errorf("nodes must be positive")
	}
	normalizedResources, err := normalizeResources(resources)
	if err != nil {
		return ModelPool{}, err
	}
	normalizedKVCache, err := normalizeKVCache(kvCache)
	if err != nil {
		return ModelPool{}, err
	}

	normalizedFeatures, err := normalizeModelFeatures(features)
	if err != nil {
		return ModelPool{}, err
	}
	tokenizer := spec.Tokenizer
	if tokenizer == "" {
		tokenizer = spec.Model
	}
	// Match the API's omitted representation after resolving an explicit empty Pool override.
	if len(engineArgs) == 0 {
		engineArgs = nil
	}
	return ModelPool{
		Name:          name,
		DesiredGroups: replicas,
		Template: inferencev1alpha1.NormalizedPoolTemplate{
			Model:                                 spec.Model,
			Source:                                source,
			ModelRevision:                         artifactRevision,
			Tokenizer:                             tokenizer,
			TokenizerRevision:                     artifactRevision,
			Backend:                               spec.Backend,
			Role:                                  role,
			NodeCount:                             nodes,
			MemberCount:                           nodes,
			Resources:                             normalizedResources,
			MaxInputTokens:                        copyInt32(maxInputTokens),
			InternalGenerateRequestBodyLimitBytes: internalGenerateRequestBodyLimitBytes,
			Network:                               network,
			ECProfile:                             ecProfile,
			Timeouts:                              timeouts,
			KVCache:                               normalizedKVCache,
			Features:                              normalizedFeatures,
			EngineArgs:                            engineArgs.DeepCopy(),
			Profiling:                             normalizeProfiling(spec.Profiling),
		},
	}, nil
}

func normalizeProfiling(input *inferencev1alpha1.ProfilingConfig) *inferencev1alpha1.ProfilingConfig {
	if input == nil || input.Engine == "pytorch" {
		return nil
	}
	return input.DeepCopy()
}

// The remaining compiler helpers normalize user shorthand into stable Pool template fields.
// Runtime-incompatible resources, storage, timeouts, and features fail before reconciliation.
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
