// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves platform-selected vLLM connector and profiler settings into execution contracts.

package vllm

import (
	"errors"
	"fmt"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
)

const ecConnector = "ECExampleConnector"

// ECProfile binds encoder and prefill processes to one platform-selected shared cache.
type ECProfile struct {
	Name               string
	Revision           string
	Connector          string
	SharedStorageClaim string
	SharedStoragePath  string
}

// MooncakeStoreProfile identifies an externally managed Store configuration.
type MooncakeStoreProfile struct {
	Name           string
	Revision       string
	ConfigMapName  string
	ConfigMapKey   string
	PythonHashSeed string
}

// RuntimeProfiles holds the optional native profiles validated before controller startup.
type RuntimeProfiles struct {
	EC            ECProfile
	MooncakeStore MooncakeStoreProfile
}

// Validate checks optional native profiles before the controller resolves model intent.
func (profiles RuntimeProfiles) Validate() error {
	if err := profiles.EC.Validate(); err != nil {
		return err
	}
	return profiles.MooncakeStore.Validate()
}

// ResolveKVRuntime compiles cache intent while retaining the selected Store's binding identity.
func ResolveKVRuntime(template api.NormalizedPoolTemplate, profile *MooncakeStoreProfile) (*api.ModelGroupKVRuntimeConfig, error) {
	if template.KVCache == nil {
		return nil, nil
	}
	if offload := template.KVCache.Offload; offload != nil {
		bytes, err := offload.Bytes()
		if err != nil {
			return nil, err
		}
		return &api.ModelGroupKVRuntimeConfig{Offload: &api.ModelGroupKVOffloadRuntime{
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
		return &api.ModelGroupKVRuntimeConfig{MooncakeStore: &api.ModelGroupMooncakeStoreRuntime{ProfileName: "kvservice:" + binding.Name, KVServiceUID: binding.UID, ProfileRevision: binding.BindingRevision, ConfigMapName: binding.ConfigMapName, ConfigMapKey: binding.ConfigMapKey, PythonHashSeed: "0", RequesterBufferBytes: binding.RequesterBufferBytes}}, nil
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
	return &api.ModelGroupKVRuntimeConfig{MooncakeStore: &api.ModelGroupMooncakeStoreRuntime{ProfileName: profile.Name, ProfileRevision: profile.Revision, ConfigMapName: profile.ConfigMapName, ConfigMapKey: profile.ConfigMapKey, PythonHashSeed: profile.PythonHashSeed}}, nil
}

// ResolveECRuntime selects the encoder/prefill profile without acquiring its Kubernetes storage.
func ResolveECRuntime(template api.NormalizedPoolTemplate, profile *ECProfile) (*api.ModelGroupECRuntimeConfig, error) {
	if template.Role != api.ModelRoleEncoder && template.Role != api.ModelRolePrefill {
		return nil, nil
	}
	if template.Role == api.ModelRolePrefill && template.ECProfile == "" {
		// P/D without an encoder keeps its EC-free transport path.
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
	role := api.ECTransferRoleConsumer
	if template.Role == api.ModelRoleEncoder {
		role = api.ECTransferRoleProducer
	}
	return &api.ModelGroupECRuntimeConfig{
		Generation:  template.EncoderCacheGeneration,
		ProfileName: profile.Name, ProfileRevision: profile.Revision,
		Connector: profile.Connector, Role: role,
		SharedStorageClaim: profile.SharedStorageClaim,
		SharedStoragePath:  profile.SharedStoragePath,
	}, nil
}

// ResolveProfilingImage selects the native profiler image and checks its device and storage needs.
// An explicit application image remains authoritative; the resolver owns the resulting workload.
func ResolveProfilingImage(template api.NormalizedPoolTemplate, image, nsightImage, deviceResourceName string) (string, error) {
	if template.Profiling == nil {
		return image, nil
	}
	switch template.Profiling.Engine {
	case "nsight":
		if template.Application == nil {
			if nsightImage == "" {
				return "", fmt.Errorf("Nsight Systems image is not configured; set runtime.vllm.nsightImage")
			}
			image = nsightImage
		}
		if deviceResourceName != "nvidia.com/gpu" {
			return "", fmt.Errorf("Nsight Systems requires NVIDIA GPUs")
		}
		if template.RuntimeCache == nil {
			return "", fmt.Errorf("Nsight Systems requires a persistent RuntimeCache")
		}
	case "mctracer":
		if deviceResourceName != "metax-tech.com/gpu" {
			return "", fmt.Errorf("mcTracer requires MetaX GPUs")
		}
		if template.RuntimeCache == nil {
			return "", fmt.Errorf("mcTracer requires a persistent RuntimeCache")
		}
	}
	return image, nil
}

// Validate checks the encoder profile consumed by startup and Group resolution.
func (profile ECProfile) Validate() error {
	if profile.Name == "" {
		if profile.Revision != "" || profile.SharedStorageClaim != "" {
			return errors.New("vLLM EC settings require vllm-ec-profile-name")
		}
		return nil
	}
	if profile.Revision == "" || profile.Connector != ecConnector || profile.SharedStorageClaim == "" || profile.SharedStoragePath == "" {
		return errors.New("vLLM EC profile is incomplete")
	}
	return nil
}

// Validate checks the external Store binding shared by startup and Group resolution.
func (profile MooncakeStoreProfile) Validate() error {
	if profile.Name == "" {
		if profile.Revision != "" || profile.ConfigMapName != "" || profile.ConfigMapKey != "" || profile.PythonHashSeed != "" {
			return errors.New("Mooncake Store settings require vllm-mooncake-store-profile-name")
		}
		return nil
	}
	if profile.Revision == "" || profile.ConfigMapName == "" || profile.ConfigMapKey == "" || profile.PythonHashSeed == "" {
		return errors.New("Mooncake Store profile is incomplete")
	}
	return nil
}
