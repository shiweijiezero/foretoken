// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compiles ModelService intent into deterministic ModelPool templates.

package compiler

import (
	"bytes"
	"encoding/json"
	"fmt"
	"path"
	"slices"
	"sort"
	"strconv"
	"strings"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/vllm"
	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	utilvalidation "k8s.io/apimachinery/pkg/util/validation"
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
	if spec.Backend == "vllm-omni" {
		if slices.ContainsFunc(spec.ModelPools, func(pool inferencev1alpha1.ModelPoolTemplate) bool { return pool.Speculation != nil }) {
			return nil, fmt.Errorf("speculation requires the vllm backend")
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
	internalGenerateRequestBodyLimitBytes := valueOrDefaultInt64(spec.InternalGenerateRequestBodyLimitBytes, inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes)
	if internalGenerateRequestBodyLimitBytes < inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes || internalGenerateRequestBodyLimitBytes > inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes {
		return nil, fmt.Errorf("internalGenerateRequestBodyLimitBytes must be between %d and %d", inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes, inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes)
	}
	if err := validateSpeculation(spec); err != nil {
		return nil, err
	}
	if len(spec.ModelPools) == 0 {
		replicas := valueOrDefault(spec.Replicas, 1)
		nodes := valueOrDefault(spec.Nodes, 1)
		pool, err := compilePool(spec, source, artifactRevision, defaultPoolName, inferencev1alpha1.ModelRoleAggregate, replicas, nodes, "", "", *spec.Resources, spec.NodeSelector, spec.EngineArgs, spec.Env, spec.MaxInputTokens, internalGenerateRequestBodyLimitBytes, spec.KVCache, spec.Features, timeouts)
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
		environment := spec.Env
		if entry.Env != nil {
			environment = *entry.Env
		}
		nodeSelector := spec.NodeSelector
		if entry.NodeSelector != nil {
			nodeSelector = entry.NodeSelector
		}
		pool, err := compilePool(spec, source, artifactRevision, entry.Name, role, replicas, nodes, entry.Network, ecProfileForRole(spec.ECProfile, role), entry.Resources, nodeSelector, engineArgs, environment, entry.MaxInputTokens, internalGenerateRequestBodyLimitBytes, entry.KVCache, entry.Features, timeouts)
		if err != nil {
			return nil, fmt.Errorf("modelPools %q: %w", entry.Name, err)
		}
		if entry.Speculation != nil {
			pool.Template.SpeculationRole = inferencev1alpha1.SpeculationRoleTarget
			pool.Template.DraftPools = slices.Clone(entry.Speculation.DraftPools)
			slices.Sort(pool.Template.DraftPools)
		} else if entry.Model != "" {
			pool.Template.SpeculationRole = inferencev1alpha1.SpeculationRoleDraft
		}
		if entry.Model != "" {
			if source != inferencev1alpha1.ModelSourceLocal && path.IsAbs(entry.Model) {
				return nil, fmt.Errorf("modelPools %q: absolute draft model paths require source local", entry.Name)
			}
			pool.Template.Model = entry.Model
		}
		if pool.Template.SpeculationRole != "" && source == inferencev1alpha1.ModelSourceLocal {
			if !path.IsAbs(pool.Template.Model) || !path.IsAbs(pool.Template.Tokenizer) {
				return nil, fmt.Errorf("modelPools %q: local DT model and tokenizer require absolute container paths", entry.Name)
			}
		}
		pools = append(pools, pool)
	}

	if err := compileNeuralSpeculation(spec, pools); err != nil {
		return nil, err
	}
	sort.Slice(pools, func(i, j int) bool { return pools[i].Name < pools[j].Name })
	return pools, nil
}

// compileNeuralSpeculation derives each Target's profile from its referenced Draft pools.
// Native neural adapters currently require one profile per engine; independent Draft models
// can differ. All roles retain independent resources and replica lifecycles.
func compileNeuralSpeculation(spec inferencev1alpha1.ModelServiceSpec, pools []ModelPool) error {
	profiles := make(map[string]*apiextensionsv1.JSON)
	for index := range pools {
		draft := &pools[index].Template
		if draft.SpeculationRole != inferencev1alpha1.SpeculationRoleDraft {
			continue
		}
		profile, err := vllm.CompileDraftSpeculation(spec.Model, draft.Model, draft.EngineArgs)
		if err != nil {
			return fmt.Errorf("modelPools %q: %w", pools[index].Name, err)
		}
		profiles[pools[index].Name] = profile
		if profile != nil {
			draft.EngineArgs = draft.EngineArgs.DeepCopy()
			delete(draft.EngineArgs, "speculative_config")
			draft.EngineArgs["speculative-config"] = *profile.DeepCopy()
			draft.Model = spec.Model
		}
	}
	for index := range pools {
		target := &pools[index].Template
		if target.SpeculationRole != inferencev1alpha1.SpeculationRoleTarget {
			continue
		}
		profile := profiles[target.DraftPools[0]]
		for _, name := range target.DraftPools[1:] {
			other := profiles[name]
			if (profile == nil) != (other == nil) || profile != nil && !bytes.Equal(profile.Raw, other.Raw) {
				return fmt.Errorf("modelPools %q: this Target adapter requires the same neural speculative-config for all referenced Draft pools", pools[index].Name)
			}
		}
		if profile == nil {
			continue
		}
		target.EngineArgs = target.EngineArgs.DeepCopy()
		if target.EngineArgs == nil {
			target.EngineArgs = make(inferencev1alpha1.EngineArguments)
		}
		target.EngineArgs["speculative-config"] = *profile.DeepCopy()
	}
	return nil
}

// validateSpeculation checks Target references before deriving roles or native profiles.
func validateSpeculation(spec inferencev1alpha1.ModelServiceSpec) error {
	byName := make(map[string]inferencev1alpha1.ModelPoolTemplate)
	draftPools := make(map[string]bool)
	enabled := false
	for _, pool := range spec.ModelPools {
		byName[pool.Name] = pool
	}
	for _, target := range spec.ModelPools {
		if target.Speculation == nil {
			continue
		}
		enabled = true
		if len(target.Speculation.DraftPools) == 0 || target.Model != "" {
			return fmt.Errorf("modelPools %q: Target speculation requires draftPools and cannot override model", target.Name)
		}
		for _, name := range target.Speculation.DraftPools {
			draft, exists := byName[name]
			if !exists || draft.Model == "" || draft.Speculation != nil {
				return fmt.Errorf("modelPools %q: draftPools %q must reference a Draft pool with model weights", target.Name, name)
			}
			draftPools[name] = true
		}
	}
	for _, pool := range spec.ModelPools {
		if pool.Model != "" && !draftPools[pool.Name] {
			return fmt.Errorf("modelPools %q: model override requires a referencing Target speculation.draftPools", pool.Name)
		}
		if !enabled {
			continue
		}
		if pool.Role != "" && pool.Role != inferencev1alpha1.ModelRoleAggregate {
			return fmt.Errorf("speculation currently requires aggregate modelPools")
		}
		if !draftPools[pool.Name] && pool.Speculation == nil {
			return fmt.Errorf("modelPools %q: a DT Target must declare speculation.draftPools", pool.Name)
		}
		if pool.Speculation != nil {
			args := spec.EngineArgs
			if pool.EngineArgs != nil {
				args = *pool.EngineArgs
			}
			for name := range args {
				if strings.ReplaceAll(name, "_", "-") == "speculative-config" {
					return fmt.Errorf("modelPools %q: configure speculative-config on its referenced Draft pools", pool.Name)
				}
			}
		}
	}
	return nil
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

func compilePool(spec inferencev1alpha1.ModelServiceSpec, source inferencev1alpha1.ModelSource, artifactRevision, name string, role inferencev1alpha1.ModelRole, replicas, nodes int32, network, ecProfile string, resources inferencev1alpha1.ModelResources, nodeSelector map[string]string, engineArgs inferencev1alpha1.EngineArguments, environment inferencev1alpha1.ModelEnvironment, maxInputTokens *int32, internalGenerateRequestBodyLimitBytes int64, kvCache *inferencev1alpha1.KVCache, features *inferencev1alpha1.ModelFeatures, timeouts inferencev1alpha1.ModelTimeouts) (ModelPool, error) {
	if nodes < 1 {
		return ModelPool{}, fmt.Errorf("nodes must be positive")
	}
	env, err := normalizeEnvironment(environment)
	if err != nil {
		return ModelPool{}, err
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
		Name:              name,
		DesiredGroups:     replicas,
		MaxInputTokens:    copyInt32(maxInputTokens),
		InstanceAdmission: spec.InstanceAdmission.DeepCopy(),
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
			NodeSelector:                          normalizedNodeSelector,
			InternalGenerateRequestBodyLimitBytes: internalGenerateRequestBodyLimitBytes,
			Network:                               network,
			ECProfile:                             ecProfile,
			Timeouts:                              timeouts,
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
