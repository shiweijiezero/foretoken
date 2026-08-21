// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves normalized ModelPool templates into immutable ModelGroup contracts.

package resolver

import (
	"fmt"

	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/util/validation"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	sglangconfig "github.com/shiweijiezero/foretoken/control-plane/internal/sglang"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/vllm"
)

// MooncakePDProfile contains opaque platform-owned Mooncake P/D profile identity and settings.
// MooncakeStoreProfile contains opaque platform-owned external Store settings.
type MooncakeStoreProfile struct {
	Name           string
	Revision       string
	ConfigMapName  string
	ConfigMapKey   string
	PythonHashSeed string
}

type MooncakePDProfile struct {
	Name                       string
	Revision                   string
	Protocol                   string
	BootstrapPort              int32
	AbortRequestTimeoutSeconds int32
	RDMADeviceName             string
	RDMAResourceName           string
	RDMAResourceCount          int32
}

// ECProfile contains the fixed platform-owned vLLM encoder/prefill connector contract.
type ECProfile struct {
	Name               string
	Revision           string
	Connector          string
	SharedStorageClaim string
	SharedStoragePath  string
}

// RuntimeProfile contains platform-owned values for the initial inference
// engine runtime profile. vLLM and SGLang image settings are independent.
type RuntimeProfile struct {
	Revision           string
	VllmImage          string
	ModelServerPort    int32
	DeviceResourceName string
	RuntimeClassName   string
	NodeSelectorKey    string
	NodeSelectorValue  string
	MooncakePD         *MooncakePDProfile
	EC                 *ECProfile
	MooncakeStore      *MooncakeStoreProfile
	SglangImage        string
}

// StaticModelPoolResolver resolves every Pool with one platform runtime profile.
// Alternative resolvers may select profiles from the Pool template.
type StaticModelPoolResolver struct {
	RuntimeProfile RuntimeProfile
}

func (resolver StaticModelPoolResolver) Resolve(template inferencev1alpha1.NormalizedPoolTemplate) (ModelGroupTemplate, error) {
	return ResolveModelPool(template, resolver.RuntimeProfile)
}

// ModelGroupTemplate is a resolved Group contract without Pool identity or ordinal.
type ModelGroupTemplate struct {
	Revision       string
	Role           inferencev1alpha1.ModelRole
	Artifacts      inferencev1alpha1.ModelGroupArtifacts
	Runtime        inferencev1alpha1.ModelGroupRuntime
	PDRuntime      *inferencev1alpha1.ModelGroupPDRuntimeConfig
	ECRuntime      *inferencev1alpha1.ModelGroupECRuntimeConfig
	KVRuntime      *inferencev1alpha1.ModelGroupKVRuntimeConfig
	Resources      inferencev1alpha1.ModelResources
	Timeouts       inferencev1alpha1.ModelTimeouts
	NodeCount      int32
	MemberCount    int32
	Parallelism    inferencev1alpha1.CompiledParallelism
	MaxInputTokens *int32
	Features       inferencev1alpha1.ModelFeatures
	Accelerator    inferencev1alpha1.ModelGroupAccelerator
	Network        string
}

// ResolveModelPool resolves one supported inference-engine execution profile into
// a Group contract. The engine-specific compile, topology, and storage resolution
// are dispatched on the template backend.
func ResolveModelPool(template inferencev1alpha1.NormalizedPoolTemplate, profile RuntimeProfile) (ModelGroupTemplate, error) {
	if template.Role != inferencev1alpha1.ModelRoleAggregate && template.Role != inferencev1alpha1.ModelRoleEncoder && template.Role != inferencev1alpha1.ModelRolePrefill && template.Role != inferencev1alpha1.ModelRoleDecode {
		return ModelGroupTemplate{}, fmt.Errorf("ModelPool role %q is not supported", template.Role)
	}
	if template.NodeCount != 1 || template.MemberCount != 1 {
		return ModelGroupTemplate{}, fmt.Errorf("only single-member inference-engine Groups are currently supported")
	}
	if errors := validation.IsDNS1123Label(profile.Revision); len(errors) > 0 || len(profile.Revision) > 16 {
		return ModelGroupTemplate{}, fmt.Errorf("inference engine profile revision must be a DNS label of at most 16 characters")
	}
	if profile.ModelServerPort < 1 || profile.ModelServerPort > 65535 {
		return ModelGroupTemplate{}, fmt.Errorf("model-server port must be between 1 and 65535")
	}
	if profile.DeviceResourceName == "" {
		return ModelGroupTemplate{}, fmt.Errorf("inference engine accelerator resource name is not configured")
	}
	if (profile.NodeSelectorKey == "") != (profile.NodeSelectorValue == "") {
		return ModelGroupTemplate{}, fmt.Errorf("GPU node selector key and value must be configured together")
	}

	engine, err := compileEngine(template, profile)
	if err != nil {
		return ModelGroupTemplate{}, err
	}

	resources := *template.Resources.DeepCopy()

	nodeSelector := map[string]string(nil)
	if profile.NodeSelectorKey != "" {
		nodeSelector = map[string]string{profile.NodeSelectorKey: profile.NodeSelectorValue}
	}

	resolved := ModelGroupTemplate{
		Role:      template.Role,
		Artifacts: engine.artifacts,
		Runtime: inferencev1alpha1.ModelGroupRuntime{
			Backend:                               template.Backend,
			Image:                                 engine.image,
			Port:                                  profile.ModelServerPort,
			Args:                                  engine.args,
			InternalGenerateRequestBodyLimitBytes: template.InternalGenerateRequestBodyLimitBytes,
		},
		PDRuntime:      engine.pdRuntime,
		ECRuntime:      engine.ecRuntime,
		KVRuntime:      engine.kvRuntime,
		Resources:      resources,
		Timeouts:       template.Timeouts,
		NodeCount:      template.NodeCount,
		MemberCount:    template.MemberCount,
		Parallelism:    engine.parallelism,
		MaxInputTokens: copyInt32(template.MaxInputTokens),
		Features:       *template.Features.DeepCopy(),
		Accelerator: inferencev1alpha1.ModelGroupAccelerator{
			DeviceResourceName: profile.DeviceResourceName,
			RuntimeClassName:   profile.RuntimeClassName,
			NodeSelector:       nodeSelector,
		},
		Network: template.Network,
	}
	resolved.Revision = profile.Revision
	return resolved, nil
}

// compiledEngine carries the engine-specific resolved values consumed by the
// shared ModelGroupTemplate construction.
type compiledEngine struct {
	artifacts   inferencev1alpha1.ModelGroupArtifacts
	args        []inferencev1alpha1.BackendArg
	parallelism inferencev1alpha1.CompiledParallelism
	image       string
	pdRuntime   *inferencev1alpha1.ModelGroupPDRuntimeConfig
	ecRuntime   *inferencev1alpha1.ModelGroupECRuntimeConfig
	kvRuntime   *inferencev1alpha1.ModelGroupKVRuntimeConfig
}

func compileEngine(template inferencev1alpha1.NormalizedPoolTemplate, profile RuntimeProfile) (compiledEngine, error) {
	switch template.Backend {
	case "vllm":
		return compileVllm(template, profile)
	case "sglang":
		return compileSglang(template, profile)
	default:
		return compiledEngine{}, fmt.Errorf("unsupported inference backend %q", template.Backend)
	}
}

func compileVllm(template inferencev1alpha1.NormalizedPoolTemplate, profile RuntimeProfile) (compiledEngine, error) {
	if profile.VllmImage == "" {
		return compiledEngine{}, fmt.Errorf("inference engine image is not configured")
	}
	effective, err := vllmconfig.Compile(template)
	if err != nil {
		return compiledEngine{}, err
	}
	if effective.Revision == "" {
		return compiledEngine{}, fmt.Errorf("vLLM --revision is required before ModelGroup creation")
	}
	pdRuntime, err := resolvePDRuntime(template, effective.Parallelism, profile.MooncakePD)
	if err != nil {
		return compiledEngine{}, err
	}
	ecRuntime, err := resolveECRuntime(template, effective.Parallelism, profile.EC)
	if err != nil {
		return compiledEngine{}, err
	}
	kvRuntime, err := resolveKVRuntime(template, profile.MooncakeStore)
	if err != nil {
		return compiledEngine{}, err
	}
	if pdRuntime != nil && kvRuntime != nil && kvRuntime.Offload != nil {
		return compiledEngine{}, fmt.Errorf("Mooncake P/D does not support local KV offload")
	}
	return compiledEngine{
		artifacts: inferencev1alpha1.ModelGroupArtifacts{
			Model:             effective.Model,
			ModelRevision:     effective.Revision,
			Tokenizer:         effective.Tokenizer,
			TokenizerRevision: effective.TokenizerRevision,
		},
		args:        effective.ExtraArgs,
		parallelism: effective.Parallelism,
		image:       profile.VllmImage,
		pdRuntime:   pdRuntime,
		ecRuntime:   ecRuntime,
		kvRuntime:   kvRuntime,
	}, nil
}

func compileSglang(template inferencev1alpha1.NormalizedPoolTemplate, profile RuntimeProfile) (compiledEngine, error) {
	if profile.SglangImage == "" {
		return compiledEngine{}, fmt.Errorf("sglang inference engine image is not configured")
	}
	effective, err := sglangconfig.Compile(template)
	if err != nil {
		return compiledEngine{}, err
	}
	return compiledEngine{
		artifacts: inferencev1alpha1.ModelGroupArtifacts{
			Model:             effective.Model,
			ModelRevision:     effective.Revision,
			Tokenizer:         effective.Model,
			TokenizerRevision: effective.Revision,
		},
		args: effective.ExtraArgs,
		parallelism: inferencev1alpha1.CompiledParallelism{
			TP: effective.TP, PP: 1, DP: effective.DP, PCP: 1, DCP: 1,
		},
		image: profile.SglangImage,
	}, nil
}

func resolveKVRuntime(template inferencev1alpha1.NormalizedPoolTemplate, profile *MooncakeStoreProfile) (*inferencev1alpha1.ModelGroupKVRuntimeConfig, error) {
	if template.KVCache == nil {
		return nil, nil
	}
	if template.KVCache.Offload != nil {
		quantity, err := resource.ParseQuantity(string(template.KVCache.Offload.CPUCacheSize))
		bytes, exact := quantity.AsInt64()
		if err != nil || !exact || bytes <= 0 {
			return nil, fmt.Errorf("KV offload cpuCacheSize must be a positive integer number of bytes")
		}
		return &inferencev1alpha1.ModelGroupKVRuntimeConfig{Offload: &inferencev1alpha1.ModelGroupKVOffloadRuntime{
			CPUBytes: bytes, Filesystem: template.KVCache.Offload.Filesystem,
		}}, nil
	}
	if template.KVCache.MooncakeStore == nil {
		return nil, fmt.Errorf("KV cache intent is incomplete")
	}
	store := template.KVCache.MooncakeStore
	if store.ManagedBinding != nil {
		binding := store.ManagedBinding
		if binding.Name == "" || binding.UID == "" || binding.BindingRevision == "" || binding.ConfigMapName == "" || binding.ConfigMapKey == "" || binding.RequesterBufferBytes < 1 {
			return nil, fmt.Errorf("managed Mooncake Store binding is incomplete")
		}
		if err := resourcevalidation.ValidateRequesterBufferBudget(template.Resources, binding.RequesterBufferBytes); err != nil {
			return nil, fmt.Errorf("managed Mooncake Store requester buffer: %w", err)
		}
		return &inferencev1alpha1.ModelGroupKVRuntimeConfig{MooncakeStore: &inferencev1alpha1.ModelGroupMooncakeStoreRuntime{ProfileName: "kvservice:" + binding.Name, KVServiceUID: binding.UID, ProfileRevision: binding.BindingRevision, ConfigMapName: binding.ConfigMapName, ConfigMapKey: binding.ConfigMapKey, PythonHashSeed: "0", RequesterBufferBytes: binding.RequesterBufferBytes}}, nil
	}
	if profile == nil || profile.Name == "" || profile.Revision == "" || profile.ConfigMapName == "" || profile.ConfigMapKey == "" || profile.PythonHashSeed == "" {
		return nil, fmt.Errorf("Mooncake Store runtime profile is incomplete")
	}
	if store.Profile != profile.Name {
		return nil, fmt.Errorf("Mooncake Store profile %q is not configured", store.Profile)
	}
	return &inferencev1alpha1.ModelGroupKVRuntimeConfig{MooncakeStore: &inferencev1alpha1.ModelGroupMooncakeStoreRuntime{ProfileName: profile.Name, ProfileRevision: profile.Revision, ConfigMapName: profile.ConfigMapName, ConfigMapKey: profile.ConfigMapKey, PythonHashSeed: profile.PythonHashSeed}}, nil
}

func resolveECRuntime(template inferencev1alpha1.NormalizedPoolTemplate, parallelism inferencev1alpha1.CompiledParallelism, profile *ECProfile) (*inferencev1alpha1.ModelGroupECRuntimeConfig, error) {
	if template.Role != inferencev1alpha1.ModelRoleEncoder && template.Role != inferencev1alpha1.ModelRolePrefill {
		return nil, nil
	}
	if template.Role == inferencev1alpha1.ModelRolePrefill && template.ECProfile == "" {
		// Legacy P/D remains an EC-free transport path.
		return nil, nil
	}
	if template.ECProfile == "" {
		return nil, fmt.Errorf("encoder ModelPool EC profile is required")
	}
	if template.NodeCount != 1 || template.MemberCount != 1 || parallelism.TP != 1 || parallelism.PP != 1 || parallelism.DP != 1 || parallelism.PCP != 1 || parallelism.DCP != 1 || parallelism.EP != nil {
		return nil, fmt.Errorf("E/P/D requires a single member/node and TP=PP=DP=PCP=DCP=1 without expert parallelism")
	}
	if profile == nil || profile.Name == "" || profile.Revision == "" || profile.Connector != "ECExampleConnector" || profile.SharedStorageClaim == "" || profile.SharedStoragePath == "" {
		return nil, fmt.Errorf("EC runtime profile is incomplete")
	}
	if template.ECProfile != profile.Name {
		return nil, fmt.Errorf("EC profile %q is not configured", template.ECProfile)
	}
	role := inferencev1alpha1.ECTransferRoleConsumer
	if template.Role == inferencev1alpha1.ModelRoleEncoder {
		role = inferencev1alpha1.ECTransferRoleProducer
	}
	return &inferencev1alpha1.ModelGroupECRuntimeConfig{
		ProfileName: profile.Name, ProfileRevision: profile.Revision,
		Connector: profile.Connector, Role: role,
		SharedStorageClaim: profile.SharedStorageClaim,
		SharedStoragePath:  profile.SharedStoragePath,
	}, nil
}

func resolvePDRuntime(template inferencev1alpha1.NormalizedPoolTemplate, parallelism inferencev1alpha1.CompiledParallelism, profile *MooncakePDProfile) (*inferencev1alpha1.ModelGroupPDRuntimeConfig, error) {
	if template.Role == inferencev1alpha1.ModelRoleAggregate || template.Role == inferencev1alpha1.ModelRoleEncoder {
		return nil, nil
	}
	if template.NodeCount != 1 || template.MemberCount != 1 || parallelism.TP != 1 || parallelism.PP != 1 || parallelism.DP != 1 || parallelism.PCP != 1 || parallelism.DCP != 1 || parallelism.EP != nil {
		return nil, fmt.Errorf("Mooncake P/D requires a single member/node and TP=PP=DP=PCP=DCP=1 without expert parallelism")
	}
	if profile == nil || profile.Name == "" || profile.Revision == "" || profile.Protocol == "" || profile.BootstrapPort < 1 || profile.BootstrapPort > 65535 || profile.AbortRequestTimeoutSeconds < 1 || profile.RDMADeviceName == "" || profile.RDMAResourceName == "" || profile.RDMAResourceCount < 1 {
		return nil, fmt.Errorf("Mooncake P/D runtime profile is incomplete")
	}
	if profile.Protocol != "rdma" {
		return nil, fmt.Errorf("Mooncake P/D protocol %q is not supported", profile.Protocol)
	}
	return &inferencev1alpha1.ModelGroupPDRuntimeConfig{
		ProfileName:                profile.Name,
		ProfileRevision:            profile.Revision,
		Connector:                  "MooncakeConnector",
		Protocol:                   profile.Protocol,
		BootstrapPort:              profile.BootstrapPort,
		AbortRequestTimeoutSeconds: profile.AbortRequestTimeoutSeconds,
		RDMADeviceName:             profile.RDMADeviceName,
		RDMAResourceName:           profile.RDMAResourceName,
		RDMAResourceCount:          profile.RDMAResourceCount,
	}, nil
}

// Spec binds a resolved template to one Pool revision ordinal.
func (template ModelGroupTemplate) Spec(pool *inferencev1alpha1.ModelPool, ordinal int32) inferencev1alpha1.ModelGroupSpec {
	return inferencev1alpha1.ModelGroupSpec{
		ModelPoolRef:   inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)},
		Revision:       template.Revision,
		Ordinal:        ordinal,
		Role:           template.Role,
		Artifacts:      template.Artifacts,
		Runtime:        template.Runtime,
		PDRuntime:      template.PDRuntime,
		ECRuntime:      template.ECRuntime,
		KVRuntime:      template.KVRuntime,
		Resources:      template.Resources,
		Timeouts:       template.Timeouts,
		NodeCount:      template.NodeCount,
		MemberCount:    template.MemberCount,
		Parallelism:    template.Parallelism,
		MaxInputTokens: copyInt32(template.MaxInputTokens),
		Features:       *template.Features.DeepCopy(),
		Accelerator:    template.Accelerator,
		Network:        template.Network,
	}
}

func copyInt32(value *int32) *int32 {
	if value == nil {
		return nil
	}
	copied := *value
	return &copied
}
