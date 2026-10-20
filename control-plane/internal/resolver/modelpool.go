// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves normalized ModelPool templates into immutable ModelGroup contracts.

package resolver

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	vllmomniconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllmomni"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
)

// RuntimeProfile contains platform-owned values for the initial vLLM runtime profile.
type RuntimeProfile struct {
	Image                  string
	LeaderWorkerScheduling *inferencev1alpha1.LeaderWorkerScheduling
	OmniImage              string
	NsightImage            string
	AdmissionStore         *inferencev1alpha1.RedisConnection
	ModelServerPort        int32
	DeviceResourceName     string
	RuntimeClassName       string
	NodeSelectorKey        string
	NodeSelectorValue      string
	MooncakePD             *runtimeconfig.PDProfile
	RDMA                   *inferencev1alpha1.RDMAAllocation
	EC                     *runtimeconfig.ECProfile
	MooncakeStore          *runtimeconfig.MooncakeStoreProfile
}

// ModelGroupTemplate is a resolved Group contract without Pool identity or ordinal.
type ModelGroupTemplate struct {
	Revision    string
	Role        inferencev1alpha1.ModelRole
	Artifacts   inferencev1alpha1.ModelGroupArtifacts
	Runtime     inferencev1alpha1.ModelGroupRuntime
	PDRuntime   *inferencev1alpha1.ModelGroupPDRuntimeConfig
	RDMA        *inferencev1alpha1.RDMAAllocation
	ECRuntime   *inferencev1alpha1.ModelGroupECRuntimeConfig
	KVRuntime   *inferencev1alpha1.ModelGroupKVRuntimeConfig
	Resources   inferencev1alpha1.ModelResources
	Timeouts    inferencev1alpha1.ModelTimeouts
	NodeCount   int32
	MemberCount int32
	Parallelism inferencev1alpha1.CompiledParallelism
	Features    inferencev1alpha1.ModelFeatures
	Accelerator inferencev1alpha1.ModelGroupAccelerator
	Network     string
}

type resolvedModelRuntime struct {
	Image       string
	EngineArgs  inferencev1alpha1.EngineArguments
	Parallelism inferencev1alpha1.CompiledParallelism
}

// ResolveModelPool resolves one supported vLLM execution profile into a Group contract.
func ResolveModelPool(template inferencev1alpha1.NormalizedPoolTemplate, profile RuntimeProfile) (ModelGroupTemplate, error) {
	if err := runtimeconfig.ValidateSourceRevision(template.SourceRevision); err != nil {
		return ModelGroupTemplate{}, err
	}
	if template.SourceRevision != "" && template.Backend != "vllm" {
		return ModelGroupTemplate{}, fmt.Errorf("source bundles support the vLLM model-server only")
	}
	if template.Backend == vllmomniconfig.Backend {
		return resolveVLLMOmniPool(template, profile)
	}
	if template.Backend != "vllm" {
		return ModelGroupTemplate{}, fmt.Errorf("inference backend %q is not supported", template.Backend)
	}
	if template.Role != inferencev1alpha1.ModelRoleAggregate && template.Role != inferencev1alpha1.ModelRoleEncoder && template.Role != inferencev1alpha1.ModelRolePrefill && template.Role != inferencev1alpha1.ModelRoleDecode {
		return ModelGroupTemplate{}, fmt.Errorf("ModelPool role %q is not supported", template.Role)
	}
	if template.NodeCount < 1 || template.MemberCount != template.NodeCount {
		return ModelGroupTemplate{}, fmt.Errorf("vLLM Groups require one member per node")
	}
	image := profile.Image
	if template.Application != nil {
		image = template.Application.Image
	}
	if image == "" {
		return ModelGroupTemplate{}, fmt.Errorf("inference engine image is not configured")
	}
	if err := validateRuntimeProfile(profile, 65535); err != nil {
		return ModelGroupTemplate{}, err
	}
	effective, err := vllmconfig.Compile(template)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	if template.ModelRevision == "" {
		return ModelGroupTemplate{}, fmt.Errorf("vLLM --revision is required before ModelGroup creation")
	}
	pdRuntime, err := resolvePDRuntime(template, profile.MooncakePD)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	ecRuntime, err := resolveECRuntime(template, profile.EC)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	kvRuntime, err := resolveKVRuntime(template, profile.MooncakeStore)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	if pdRuntime != nil && kvRuntime != nil && kvRuntime.Offload != nil {
		return ModelGroupTemplate{}, fmt.Errorf("Mooncake P/D does not support local KV offload")
	}
	if template.Profiling != nil && template.Profiling.Engine == "nsight" {
		if template.Application == nil {
			if profile.NsightImage == "" {
				return ModelGroupTemplate{}, fmt.Errorf("Nsight Systems image is not configured; set runtime.vllm.nsightImage")
			}
			image = profile.NsightImage
		}
		if profile.DeviceResourceName != "nvidia.com/gpu" {
			return ModelGroupTemplate{}, fmt.Errorf("Nsight Systems requires NVIDIA GPUs")
		}
		if template.RuntimeCache == nil {
			return ModelGroupTemplate{}, fmt.Errorf("Nsight Systems requires a persistent RuntimeCache")
		}
	}
	rdmaRequired := effective.RequiresRDMA()
	if rdmaRequired && profile.RDMA == nil {
		return ModelGroupTemplate{}, fmt.Errorf("engineArgs.all2all-backend=%q requires a platform RDMA allocation", effective.Parallelism.EP.Backend)
	}
	var rdma *inferencev1alpha1.RDMAAllocation
	if template.NodeCount > 1 || rdmaRequired || (pdRuntime != nil && pdRuntime.Protocol == "rdma") || effective.RequestsWeightSharing(template, profile.DeviceResourceName) {
		rdma = profile.RDMA.DeepCopy()
	}
	if pdRuntime != nil && pdRuntime.Protocol == "rdma" && rdma == nil {
		return ModelGroupTemplate{}, fmt.Errorf("Mooncake P/D with RDMA transport requires a platform RDMA allocation")
	}
	if rdma != nil && (rdma.ResourceName == "" || rdma.ResourceCount < 1) {
		return ModelGroupTemplate{}, fmt.Errorf("platform RDMA allocation is incomplete")
	}
	effective.ConfigureWeightSharing(template, profile.DeviceResourceName, rdma, profile.ModelServerPort, pdRuntime)
	if template.Profiling != nil && template.Profiling.Engine == "mctracer" {
		if profile.DeviceResourceName != "metax-tech.com/gpu" {
			return ModelGroupTemplate{}, fmt.Errorf("mcTracer requires MetaX GPUs")
		}
		if template.RuntimeCache == nil {
			return ModelGroupTemplate{}, fmt.Errorf("mcTracer requires a persistent RuntimeCache")
		}
	}
	nodeSelector, err := mergeNodeSelectors(template.NodeSelector, profile)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	resolved := projectModelGroupTemplate(template, profile, nodeSelector, resolvedModelRuntime{
		Image: image, EngineArgs: effective.EngineArgs, Parallelism: effective.Parallelism,
	})
	resolved.Runtime.TritonCacheDirectory = vllmconfig.TritonCacheDirectory(template.RuntimeCache, resolved.Runtime.PreparationVersion)
	resolved.Runtime.Profiling = template.Profiling.DeepCopy()
	resolved.PDRuntime, resolved.RDMA, resolved.ECRuntime, resolved.KVRuntime = pdRuntime, rdma, ecRuntime, kvRuntime
	return resolved, nil
}

// resolveVLLMOmniPool binds native Omni options and platform resources into an immutable execution contract.
func resolveVLLMOmniPool(template inferencev1alpha1.NormalizedPoolTemplate, profile RuntimeProfile) (ModelGroupTemplate, error) {
	image := profile.OmniImage
	if template.Application != nil {
		image = template.Application.Image
	}
	if image == "" {
		return ModelGroupTemplate{}, fmt.Errorf("vLLM-Omni inference engine image is not configured")
	}
	if err := validateRuntimeProfile(profile, 65533); err != nil {
		return ModelGroupTemplate{}, err
	}
	effective, err := vllmomniconfig.Compile(template)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	if template.ModelRevision == "" {
		return ModelGroupTemplate{}, fmt.Errorf("vLLM-Omni modelRevision is required before ModelGroup creation")
	}
	nodeSelector, err := mergeNodeSelectors(template.NodeSelector, profile)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	resolved := projectModelGroupTemplate(template, profile, nodeSelector, resolvedModelRuntime{
		Image: image, EngineArgs: effective.EngineArgs, Parallelism: effective.Parallelism,
	})
	// Omni uses the same vLLM compiler-cache layout while retaining its own launch contract.
	resolved.Runtime.TritonCacheDirectory = vllmconfig.TritonCacheDirectory(template.RuntimeCache, resolved.Runtime.PreparationVersion)
	return resolved, nil
}

func mergeNodeSelectors(userSelector map[string]string, profile RuntimeProfile) (map[string]string, error) {
	selector := make(map[string]string, len(userSelector)+1)
	for key, value := range userSelector {
		selector[key] = value
	}
	if profile.NodeSelectorKey == "" {
		if len(selector) == 0 {
			return nil, nil
		}
		return selector, nil
	}
	if value, exists := selector[profile.NodeSelectorKey]; exists && value != profile.NodeSelectorValue {
		return nil, fmt.Errorf("nodeSelector %q=%q conflicts with platform accelerator selector %q=%q", profile.NodeSelectorKey, value, profile.NodeSelectorKey, profile.NodeSelectorValue)
	}
	selector[profile.NodeSelectorKey] = profile.NodeSelectorValue
	return selector, nil
}

func validateRuntimeProfile(profile RuntimeProfile, maxPort int32) error {
	if profile.ModelServerPort < 1 || profile.ModelServerPort > maxPort {
		return fmt.Errorf("model-server port must be between 1 and %d", maxPort)
	}
	if profile.DeviceResourceName == "" {
		return fmt.Errorf("inference engine accelerator resource name is not configured")
	}
	if (profile.NodeSelectorKey == "") != (profile.NodeSelectorValue == "") {
		return fmt.Errorf("GPU node selector key and value must be configured together")
	}
	return nil
}

func projectModelGroupTemplate(template inferencev1alpha1.NormalizedPoolTemplate, profile RuntimeProfile, nodeSelector map[string]string, runtime resolvedModelRuntime) ModelGroupTemplate {
	applicationURL := ""
	var selectedPreparationVersion *int32
	var admissionStore *inferencev1alpha1.RedisConnection
	var leaderWorkerScheduling *inferencev1alpha1.LeaderWorkerScheduling
	var instanceAdmissionProtocol uint32
	if template.Application != nil {
		selectedPreparationVersion = template.Application.PreparationVersion
		if template.NodeCount > 1 {
			leaderWorkerScheduling = template.Application.LeaderWorkerScheduling.DeepCopy()
		}
		applicationURL = template.Application.ApplicationURL
		admissionStore = template.Application.AdmissionStore.DeepCopy()
		instanceAdmissionProtocol = template.Application.InstanceAdmissionProtocol
	}
	preparationVersion := runtimeconfig.ModelPreparationVersion(selectedPreparationVersion, template.Backend)
	var startupAdmission *inferencev1alpha1.InstanceAdmissionConfig
	if instanceAdmissionProtocol == 0 {
		startupAdmission = template.InstanceAdmission.DeepCopy()
	}
	return ModelGroupTemplate{
		Role: template.Role,
		Artifacts: inferencev1alpha1.ModelGroupArtifacts{
			Model: template.Model, Source: template.Source, ModelRevision: template.ModelRevision,
			Tokenizer: template.Tokenizer, TokenizerRevision: template.TokenizerRevision,
			Cache: template.RuntimeCache.DeepCopy(), HuggingFaceAccess: template.HuggingFaceAccess.DeepCopy(),
		},
		Runtime: inferencev1alpha1.ModelGroupRuntime{
			LeaderWorkerScheduling: leaderWorkerScheduling,
			PreparationVersion:     preparationVersion,
			SourceRevision:         template.SourceRevision,
			ApplicationURL:         applicationURL,
			Backend:                template.Backend, Image: runtime.Image, Port: profile.ModelServerPort,
			EngineArgs:                            runtime.EngineArgs,
			Env:                                   template.DeepCopy().Env,
			AdmissionStore:                        admissionStore,
			PrefixCacheIsolation:                  template.PrefixCacheIsolation,
			InstanceAdmissionProtocol:             instanceAdmissionProtocol,
			InstanceAdmission:                     startupAdmission,
			Profiling:                             template.Profiling.DeepCopy(),
			InternalGenerateRequestBodyLimitBytes: template.InternalGenerateRequestBodyLimitBytes,
		},
		Resources: *template.Resources.DeepCopy(), Timeouts: template.Timeouts,
		NodeCount: template.NodeCount, MemberCount: template.MemberCount,
		Parallelism: runtime.Parallelism,
		Features:    *template.Features.DeepCopy(),
		Accelerator: inferencev1alpha1.ModelGroupAccelerator{
			DeviceResourceName: profile.DeviceResourceName,
			RuntimeClassName:   profile.RuntimeClassName,
			NodeSelector:       nodeSelector,
		},
		Network: template.Network,
	}
}

// resolveKVRuntime binds the selected cache mode to a validated ModelGroup runtime contract.
func resolveKVRuntime(template inferencev1alpha1.NormalizedPoolTemplate, profile *runtimeconfig.MooncakeStoreProfile) (*inferencev1alpha1.ModelGroupKVRuntimeConfig, error) {
	if template.KVCache == nil {
		return nil, nil
	}
	if offload := template.KVCache.Offload; offload != nil {
		bytes, err := offload.Bytes()
		if err != nil {
			return nil, err
		}
		return &inferencev1alpha1.ModelGroupKVRuntimeConfig{Offload: &inferencev1alpha1.ModelGroupKVOffloadRuntime{
			CPUBytes: bytes, Filesystem: offload.Filesystem,
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
	if profile == nil || profile.Name == "" {
		return nil, fmt.Errorf("Mooncake Store runtime profile is incomplete")
	}
	if err := profile.Validate(); err != nil {
		return nil, err
	}
	if store.Profile != profile.Name {
		return nil, fmt.Errorf("Mooncake Store profile %q is not configured", store.Profile)
	}
	return &inferencev1alpha1.ModelGroupKVRuntimeConfig{MooncakeStore: &inferencev1alpha1.ModelGroupMooncakeStoreRuntime{ProfileName: profile.Name, ProfileRevision: profile.Revision, ConfigMapName: profile.ConfigMapName, ConfigMapKey: profile.ConfigMapKey, PythonHashSeed: profile.PythonHashSeed}}, nil
}

// resolveECRuntime resolves the platform EC profile for encoder and prefill roles.
func resolveECRuntime(template inferencev1alpha1.NormalizedPoolTemplate, profile *runtimeconfig.ECProfile) (*inferencev1alpha1.ModelGroupECRuntimeConfig, error) {
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
	if profile == nil || profile.Name == "" {
		return nil, fmt.Errorf("EC runtime profile is incomplete")
	}
	if err := profile.Validate(); err != nil {
		return nil, err
	}
	if template.ECProfile != profile.Name {
		return nil, fmt.Errorf("EC profile %q is not configured", template.ECProfile)
	}
	role := inferencev1alpha1.ECTransferRoleConsumer
	if template.Role == inferencev1alpha1.ModelRoleEncoder {
		role = inferencev1alpha1.ECTransferRoleProducer
	}
	return &inferencev1alpha1.ModelGroupECRuntimeConfig{
		Generation:  template.EncoderCacheGeneration,
		ProfileName: profile.Name, ProfileRevision: profile.Revision,
		Connector: profile.Connector, Role: role,
		SharedStorageClaim: profile.SharedStorageClaim,
		SharedStoragePath:  profile.SharedStoragePath,
	}, nil
}

// resolvePDRuntime resolves the platform Mooncake P/D profile for split serving roles.
func resolvePDRuntime(template inferencev1alpha1.NormalizedPoolTemplate, profile *runtimeconfig.PDProfile) (*inferencev1alpha1.ModelGroupPDRuntimeConfig, error) {
	if template.Role == inferencev1alpha1.ModelRoleAggregate || template.Role == inferencev1alpha1.ModelRoleEncoder {
		return nil, nil
	}
	if profile == nil || profile.Name == "" {
		return nil, fmt.Errorf("Mooncake P/D runtime profile is incomplete")
	}
	if err := profile.Validate(); err != nil {
		return nil, err
	}
	rdmaDeviceName := profile.RDMADeviceName
	if profile.Protocol == "tcp" {
		rdmaDeviceName = ""
	}
	return &inferencev1alpha1.ModelGroupPDRuntimeConfig{
		ProfileName:                profile.Name,
		ProfileRevision:            profile.Revision,
		Connector:                  "MooncakeConnector",
		Protocol:                   profile.Protocol,
		BootstrapPort:              int32(profile.BootstrapPort),
		AbortRequestTimeoutSeconds: int32(profile.AbortRequestTimeoutSeconds),
		RDMADeviceName:             rdmaDeviceName,
	}, nil
}

// Spec binds a resolved template to one Pool revision ordinal.
func (template ModelGroupTemplate) Spec(pool *inferencev1alpha1.ModelPool, ordinal int32) inferencev1alpha1.ModelGroupSpec {
	pdRuntime := template.PDRuntime.DeepCopy()
	if pdRuntime != nil {
		pdRuntime.ServiceUID = pool.Spec.ModelServiceRef.UID
	}
	ecRuntime := template.ECRuntime.DeepCopy()
	if ecRuntime != nil {
		ecRuntime.ServiceUID = pool.Spec.ModelServiceRef.UID
	}
	return inferencev1alpha1.ModelGroupSpec{
		ModelPoolRef: inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)},
		Revision:     template.Revision,
		Ordinal:      ordinal,
		Role:         template.Role,
		Artifacts:    template.Artifacts,
		Runtime:      template.Runtime,
		PDRuntime:    pdRuntime,
		RDMA:         template.RDMA,
		ECRuntime:    ecRuntime,
		KVRuntime:    template.KVRuntime,
		Resources:    template.Resources,
		Timeouts:     template.Timeouts,
		NodeCount:    template.NodeCount,
		MemberCount:  template.MemberCount,
		Parallelism:  template.Parallelism,
		Features:     *template.Features.DeepCopy(),
		Accelerator:  template.Accelerator,
		Network:      template.Network,
	}
}
