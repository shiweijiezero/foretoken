// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compiles ModelService intent into deterministic ModelPool templates.

package compiler

import (
	"bytes"
	"encoding/json"
	"fmt"
	"path"
	"sort"
	"strconv"
	"strings"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	utilvalidation "k8s.io/apimachinery/pkg/util/validation"
	"k8s.io/utils/ptr"
)

const (
	defaultPoolName           = "default"
	defaultHFRevision         = "main"
	defaultModelScopeRevision = "master"
	localArtifactRevision     = "local"
)

// ModelPool is one normalized Pool produced from ModelService intent.
type ModelPool struct {
	Name              string
	DesiredGroups     int32
	MaxInputTokens    *int32
	InstanceAdmission *inferencev1alpha1.InstanceAdmissionConfig
	Template          inferencev1alpha1.NormalizedPoolTemplate
}

// CompileModelService normalizes shorthand or advanced Pool intent without resolving platform access settings.
func CompileModelService(spec inferencev1alpha1.ModelServiceSpec) ([]ModelPool, error) {
	if err := spec.Admission.Validate(); err != nil {
		return nil, err
	}
	if spec.Source == "" {
		spec.Source = inferencev1alpha1.ModelSourceHF
	}
	artifactRevision := defaultHFRevision
	switch spec.Source {
	case inferencev1alpha1.ModelSourceLocal:
		artifactRevision = localArtifactRevision
	case inferencev1alpha1.ModelSourceHF:
	case inferencev1alpha1.ModelSourceModelScope:
		artifactRevision = defaultModelScopeRevision
	default:
		return nil, fmt.Errorf("source must be local, hf, or modelscope")
	}
	if spec.Source != inferencev1alpha1.ModelSourceLocal && (path.IsAbs(spec.Model) || path.IsAbs(spec.Tokenizer)) {
		return nil, fmt.Errorf("absolute model and tokenizer paths require source local")
	}
	timeouts, err := normalizeTimeouts(spec.Timeouts)
	if err != nil {
		return nil, err
	}
	spec.Timeouts = timeouts
	if spec.Backend == "vllm-omni" {
		if spec.PrefixCacheIsolation != nil {
			return nil, fmt.Errorf("prefixCacheIsolation is not supported by the vllm-omni video backend")
		}
		if spec.InstanceAdmission != nil {
			return nil, fmt.Errorf("instanceAdmission is not supported by the vllm-omni video backend")
		}
		if (spec.Admission != nil && (spec.Admission.MaxWaitingRequests != nil || spec.Admission.QueueTimeout != "")) || (spec.RoleRules != nil && len(*spec.RoleRules) > 0) {
			return nil, fmt.Errorf("admission and roleRules are not supported by the vllm-omni video backend")
		}
	}
	if spec.InstanceAdmission != nil && spec.InstanceAdmission.MaxConcurrentRequests == 0 {
		return nil, fmt.Errorf("instanceAdmission.maxConcurrentRequests must be positive")
	}
	internalGenerateRequestBodyLimitBytes := ptr.Deref(spec.InternalGenerateRequestBodyLimitBytes, inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes)
	if internalGenerateRequestBodyLimitBytes < inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes || internalGenerateRequestBodyLimitBytes > inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes {
		return nil, fmt.Errorf("internalGenerateRequestBodyLimitBytes must be between %d and %d", inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes, inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes)
	}
	spec.InternalGenerateRequestBodyLimitBytes = &internalGenerateRequestBodyLimitBytes
	if len(spec.ModelPools) == 0 {
		entry := inferencev1alpha1.ModelPoolTemplate{
			Name:           defaultPoolName,
			Role:           inferencev1alpha1.ModelRoleAggregate,
			Replicas:       spec.Replicas,
			Nodes:          spec.Nodes,
			Resources:      *spec.Resources,
			MaxInputTokens: spec.MaxInputTokens,
			KVCache:        spec.KVCache,
			Features:       spec.Features,
		}
		pool, err := compilePool(spec, entry, artifactRevision)
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
		pool, err := compilePool(spec, entry, artifactRevision)
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

// compilePool resolves one Pool's overrides and builds its execution template.
func compilePool(spec inferencev1alpha1.ModelServiceSpec, entry inferencev1alpha1.ModelPoolTemplate, artifactRevision string) (ModelPool, error) {
	role := entry.Role
	if role == "" {
		role = inferencev1alpha1.ModelRoleAggregate
	}
	nodes := ptr.Deref(entry.Nodes, 1)
	if nodes < 1 {
		return ModelPool{}, fmt.Errorf("nodes must be positive")
	}
	// Pool maps replace the corresponding service map as a whole, including an explicit empty map.
	engineArgs := spec.EngineArgs
	if entry.EngineArgs != nil {
		engineArgs = *entry.EngineArgs
	}
	environment := spec.Env
	if entry.Env != nil {
		environment = *entry.Env
	}
	nodeSelector := spec.NodeSelector
	if entry.NodeSelector != nil {
		nodeSelector = entry.NodeSelector
	}
	env, err := normalizeEnvironment(environment)
	if err != nil {
		return ModelPool{}, err
	}
	normalizedResources, err := normalizeResources(entry.Resources)
	if err != nil {
		return ModelPool{}, err
	}
	normalizedKVCache, err := normalizeKVCache(entry.KVCache)
	if err != nil {
		return ModelPool{}, err
	}

	normalizedFeatures, err := normalizeModelFeatures(entry.Features)
	if err != nil {
		return ModelPool{}, err
	}
	normalizedNodeSelector, err := normalizeNodeSelector(nodeSelector)
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
		Name:              entry.Name,
		DesiredGroups:     ptr.Deref(entry.Replicas, 1),
		MaxInputTokens:    copyInt32(entry.MaxInputTokens),
		InstanceAdmission: spec.InstanceAdmission.DeepCopy(),
		Template: inferencev1alpha1.NormalizedPoolTemplate{
			Model:                                 spec.Model,
			Source:                                spec.Source,
			ModelRevision:                         artifactRevision,
			Tokenizer:                             tokenizer,
			TokenizerRevision:                     artifactRevision,
			Backend:                               spec.Backend,
			Role:                                  role,
			NodeCount:                             nodes,
			MemberCount:                           nodes,
			Resources:                             normalizedResources,
			NodeSelector:                          normalizedNodeSelector,
			PrefixCacheIsolation:                  spec.PrefixCacheIsolation,
			InternalGenerateRequestBodyLimitBytes: *spec.InternalGenerateRequestBodyLimitBytes,
			Network:                               entry.Network,
			ECProfile:                             ecProfileForRole(spec.ECProfile, role),
			Timeouts:                              spec.Timeouts,
			KVCache:                               normalizedKVCache,
			Features:                              normalizedFeatures,
			EngineArgs:                            engineArgs.DeepCopy(),
			Env:                                   env,
			Profiling:                             normalizeProfiling(spec.Profiling),
		},
	}, nil
}

// normalizeEnvironment compiles public values into native EnvVars for Pool consumers.
// Dependencies are ordered before their users; kubelet remains responsible for expansion.
func normalizeEnvironment(input inferencev1alpha1.ModelEnvironment) ([]corev1.EnvVar, error) {
	names := make([]string, 0, len(input))
	for name := range input {
		names = append(names, name)
	}
	sort.Strings(names)
	variables := make(map[string]corev1.EnvVar, len(input))
	for _, name := range names {
		if errors := utilvalidation.IsRelaxedEnvVarName(name); len(errors) > 0 {
			return nil, fmt.Errorf("env name %q is invalid: %s", name, errors[0])
		}
		decoder := json.NewDecoder(bytes.NewReader(input[name].Raw))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil {
			return nil, fmt.Errorf("env %q must be a string, number, boolean, or valueFrom reference", name)
		}
		variable := corev1.EnvVar{Name: name}
		switch value := value.(type) {
		case string:
			variable.Value = value
		case json.Number:
			variable.Value = value.String()
		case bool:
			variable.Value = strconv.FormatBool(value)
		case map[string]any:
			var reference struct {
				ValueFrom *corev1.EnvVarSource `json:"valueFrom"`
			}
			decoder = json.NewDecoder(bytes.NewReader(input[name].Raw))
			decoder.DisallowUnknownFields()
			if err := decoder.Decode(&reference); err != nil || reference.ValueFrom == nil {
				return nil, fmt.Errorf("env %q must contain a native Kubernetes valueFrom reference", name)
			}
			variable.ValueFrom = reference.ValueFrom
		default:
			return nil, fmt.Errorf("env %q must be a string, number, boolean, or valueFrom reference", name)
		}
		variables[name] = variable
	}

	// Recognize only kubelet references, including its $$ escape, without expanding values.
	// Sorted roots and left-to-right dependencies make the result independent of map iteration.
	var output []corev1.EnvVar
	state := make(map[string]uint8, len(input))
	var visit func(string) error
	visit = func(name string) error {
		switch state[name] {
		case 1:
			return fmt.Errorf("env %q has a cyclic variable reference", name)
		case 2:
			return nil
		}
		state[name] = 1
		variable := variables[name]
		for remaining := variable.Value; remaining != ""; {
			i := strings.IndexByte(remaining, '$')
			if i < 0 || i+1 == len(remaining) {
				break
			}
			remaining = remaining[i+1:]
			switch remaining[0] {
			case '$':
				remaining = remaining[1:]
			case '(':
				end := strings.IndexByte(remaining, ')')
				if end < 0 {
					remaining = ""
					continue
				}
				dependency := remaining[1:end]
				if _, exists := variables[dependency]; exists {
					if err := visit(dependency); err != nil {
						return err
					}
				}
				remaining = remaining[end+1:]
			}
		}
		state[name] = 2
		output = append(output, variable)
		return nil
	}
	for _, name := range names {
		if err := visit(name); err != nil {
			return nil, err
		}
	}
	return output, nil
}

func normalizeNodeSelector(input map[string]string) (map[string]string, error) {
	if len(input) == 0 {
		return nil, nil
	}
	output := make(map[string]string, len(input))
	for key, value := range input {
		if errors := utilvalidation.IsQualifiedName(key); len(errors) > 0 {
			return nil, fmt.Errorf("nodeSelector key %q is invalid: %s", key, errors[0])
		}
		if errors := utilvalidation.IsValidLabelValue(value); len(errors) > 0 {
			return nil, fmt.Errorf("nodeSelector.%s value is invalid: %s", key, errors[0])
		}
		output[key] = value
	}
	return output, nil
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

func normalizeModelFeatures(input *inferencev1alpha1.ModelFeatures) (inferencev1alpha1.ModelFeatures, error) {
	if input == nil {
		return inferencev1alpha1.ModelFeatures{}, nil
	}
	output := inferencev1alpha1.ModelFeatures{Tools: input.Tools, Reasoning: input.Reasoning}
	structured := make(map[inferencev1alpha1.StructuredOutputFormat]struct{}, len(input.StructuredOutputs))
	for _, format := range input.StructuredOutputs {
		if format != inferencev1alpha1.StructuredOutputFormatJSONObject && format != inferencev1alpha1.StructuredOutputFormatJSONSchema && format != inferencev1alpha1.StructuredOutputFormatStructuralTag {
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
