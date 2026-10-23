// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves normalized ModelPool templates into immutable ModelGroup contracts.

package resolver

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	vllmomniconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllmomni"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
)

// RuntimeProfile supplies platform images, access, allocations, and native profiles for Pool resolution.
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
	RDMABindings           []RDMANetworkBinding
	EC                     *vllmconfig.ECProfile
	MooncakeStore          *vllmconfig.MooncakeStoreProfile
}

// RDMANetworkBinding is an installer-selected allocation and its CNI namespace scope.
// Namespace is empty only when the installed CNI allows the attachments to be shared.
type RDMANetworkBinding struct {
	Allocation inferencev1alpha1.RDMAAllocation `json:"allocation"`
	Namespace  string                           `json:"namespace,omitempty"`
	// AllowExplicitNetwork preserves overrides authorized by retained startup flags only.
	AllowExplicitNetwork bool `json:"-"`
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

// ResolveModelPool binds normalized intent to its backend and platform resources in a Group contract.
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
	pdRuntime, err := vllmconfig.ResolvePDRuntime(template, effective.Parallelism, profile.ModelServerPort)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	ecRuntime, err := vllmconfig.ResolveECRuntime(template, profile.EC)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	kvRuntime, err := vllmconfig.ResolveKVRuntime(template, profile.MooncakeStore)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	if pdRuntime != nil && kvRuntime != nil && kvRuntime.Offload != nil {
		return ModelGroupTemplate{}, fmt.Errorf("P/D does not support local KV offload")
	}
	image, err = vllmconfig.ResolveProfilingImage(template, image, profile.NsightImage, profile.DeviceResourceName)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	rdmaRequired := effective.RequiresRDMA()
	if rdmaRequired && template.RDMA == nil {
		return ModelGroupTemplate{}, fmt.Errorf("engineArgs.all2all-backend=%q requires a platform RDMA allocation", effective.Parallelism.EP.Backend)
	}
	var rdma *inferencev1alpha1.RDMAAllocation
	if effective.RequestsRDMA(template, profile.DeviceResourceName) {
		rdma = template.RDMA.DeepCopy()
	}
	if pdRuntime != nil && pdRuntime.Protocol == "rdma" && rdma == nil {
		return ModelGroupTemplate{}, fmt.Errorf("P/D with RDMA transport requires a platform RDMA allocation")
	}
	if err := runtimeconfig.ValidateRDMAAllocation(rdma, profile.DeviceResourceName); err != nil {
		return ModelGroupTemplate{}, err
	}
	rdma = rdma.Canonical()
	effective.ConfigureWeightSharing(template, profile.DeviceResourceName, rdma, profile.ModelServerPort, pdRuntime)
	nodeSelector, err := mergeNodeSelectors(template.NodeSelector, profile)
	if err != nil {
		return ModelGroupTemplate{}, err
	}
	resolved := projectModelGroupTemplate(template, profile, nodeSelector, resolvedModelRuntime{
		Image: image, EngineArgs: effective.EngineArgs, Parallelism: effective.Parallelism,
	})
	resolved.Runtime.TritonCacheDirectory = vllmconfig.TritonCacheDirectory(template.RuntimeCache, resolved.Runtime.PreparationVersion)
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

// mergeNodeSelectors combines user placement with the platform accelerator selector without mutating either.
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

// projectModelGroupTemplate freezes resolved inputs while leaving Pool identity to Spec.
// The selected application owns live admission; older applications retain startup-only settings.
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
	rdma := template.RDMA.Canonical()
	network := template.Network
	if rdma != nil {
		rdma.Networks = runtimeconfig.RDMANetworks(pool.Namespace, rdma, network)
		network = ""
	}
	return inferencev1alpha1.ModelGroupSpec{
		ModelPoolRef: inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)},
		Revision:     template.Revision,
		Ordinal:      ordinal,
		Role:         template.Role,
		Artifacts:    template.Artifacts,
		Runtime:      template.Runtime,
		PDRuntime:    pdRuntime,
		RDMA:         rdma,
		ECRuntime:    ecRuntime,
		KVRuntime:    template.KVRuntime,
		Resources:    template.Resources,
		Timeouts:     template.Timeouts,
		NodeCount:    template.NodeCount,
		MemberCount:  template.MemberCount,
		Parallelism:  template.Parallelism,
		Features:     *template.Features.DeepCopy(),
		Accelerator:  template.Accelerator,
		Network:      network,
	}
}
