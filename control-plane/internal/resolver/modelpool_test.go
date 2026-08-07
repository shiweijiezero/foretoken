// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Tests ModelPool resolution into immutable ModelGroup templates.

package resolver

import (
	"reflect"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func TestResolveModelPool(t *testing.T) {
	maxInputTokens := int32(16384)
	template := resolverTemplate()
	template.MaxInputTokens = &maxInputTokens
	resolved, err := ResolveModelPool(template, resolverProfile())
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Revision == "" || resolved.Artifacts.ModelRevision != "model-revision" {
		t.Fatalf("resolved artifacts = %#v", resolved)
	}
	if resolved.Resources.Requests.GPU.Type != "nvidia-h100-80gb" || resolved.Accelerator.NodeSelector["nvidia.com/gpu.product"] != "NVIDIA-H100-80GB-HBM3" {
		t.Fatalf("resolved accelerator = %#v", resolved)
	}
	if resolved.MaxInputTokens == nil || *resolved.MaxInputTokens != 16384 || resolved.MaxInputTokens == template.MaxInputTokens {
		t.Fatalf("resolved maxInputTokens = %#v", resolved.MaxInputTokens)
	}
	groupSpec := resolved.Spec(&inferencev1alpha1.ModelPool{}, 0)
	if groupSpec.MaxInputTokens == nil || *groupSpec.MaxInputTokens != 16384 || groupSpec.MaxInputTokens == resolved.MaxInputTokens {
		t.Fatalf("resolved ModelGroup maxInputTokens = %#v", groupSpec.MaxInputTokens)
	}
}

func TestResolveModelPoolPropagatesFeaturesAndChangesRevision(t *testing.T) {
	template := resolverTemplate()
	template.Features = inferencev1alpha1.ModelFeatures{Tools: true, StructuredOutputs: []inferencev1alpha1.StructuredOutputFormat{inferencev1alpha1.StructuredOutputFormatJSONObject}, Multimodal: []inferencev1alpha1.MultimodalModality{inferencev1alpha1.MultimodalModalityImage}}
	first, err := ResolveModelPool(template, resolverProfile())
	if err != nil {
		t.Fatal(err)
	}
	if !first.Features.Tools || len(first.Features.Multimodal) != 1 || !reflect.DeepEqual(first.Spec(&inferencev1alpha1.ModelPool{}, 0).Features, first.Features) {
		t.Fatalf("resolved features = %#v", first.Features)
	}
	template.Features.Reasoning = true
	second, err := ResolveModelPool(template, resolverProfile())
	if err != nil || second.Revision == first.Revision {
		t.Fatalf("features did not roll Group revision: %q -> %q, err = %v", first.Revision, second.Revision, err)
	}
}

func TestResolveModelPoolRejectsPDMultiModalFeatures(t *testing.T) {
	template := resolverTemplate()
	template.Role = inferencev1alpha1.ModelRolePrefill
	template.Features.Multimodal = []inferencev1alpha1.MultimodalModality{inferencev1alpha1.MultimodalModalityImage}
	if _, err := ResolveModelPool(template, resolverProfile()); err == nil {
		t.Fatal("P/D multimodal features were accepted")
	}
}

func TestResolveModelPoolMaxInputTokensChangesRevision(t *testing.T) {
	template := resolverTemplate()
	firstLimit := int32(16384)
	template.MaxInputTokens = &firstLimit
	first, err := ResolveModelPool(template, resolverProfile())
	if err != nil {
		t.Fatal(err)
	}
	secondLimit := int32(8192)
	template.MaxInputTokens = &secondLimit
	second, err := ResolveModelPool(template, resolverProfile())
	if err != nil || second.Revision == first.Revision {
		t.Fatalf("maxInputTokens did not roll Group revision: %q -> %q, err = %v", first.Revision, second.Revision, err)
	}
}

func TestResolveModelPoolRequiresModelRevision(t *testing.T) {
	template := resolverTemplate()
	template.ModelRevision = ""
	if _, err := ResolveModelPool(template, resolverProfile()); err == nil {
		t.Fatal("model without an immutable revision was accepted")
	}
}

func TestResolveModelPoolRejectsDifferentAccelerator(t *testing.T) {
	template := resolverTemplate()
	template.Resources.Requests.GPU.Type = "nvidia-a100-80gb"
	if _, err := ResolveModelPool(template, resolverProfile()); err == nil {
		t.Fatal("unsupported accelerator type was accepted")
	}
}

func resolverTemplate() inferencev1alpha1.NormalizedPoolTemplate {
	return inferencev1alpha1.NormalizedPoolTemplate{
		Model:         "Qwen/Qwen3-0.6B",
		ModelRevision: "model-revision",
		Backend:       "vllm",
		Role:          inferencev1alpha1.ModelRoleAggregate,
		NodeCount:     1,
		MemberCount:   1,
		Resources: inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
			ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
			GPU:                     inferencev1alpha1.GPURequest{Type: "auto", Count: 1},
		}},
		Parallelism: inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1},
		Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
	}
}

func resolverProfile() RuntimeProfile {
	return RuntimeProfile{
		Image:              "vllm:test",
		ModelServerPort:    9000,
		AcceleratorType:    "nvidia-h100-80gb",
		DeviceResourceName: "nvidia.com/gpu",
		NodeSelectorKey:    "nvidia.com/gpu.product",
		NodeSelectorValue:  "NVIDIA-H100-80GB-HBM3",
	}
}

func TestResolveModelPoolMaterializesMooncakePD(t *testing.T) {
	profile := resolverProfile()
	profile.MooncakePD = &MooncakePDProfile{Name: "cluster-pd", Revision: "release-A", Protocol: "rdma", BootstrapPort: 29001, AbortRequestTimeoutSeconds: 30}
	for _, role := range []inferencev1alpha1.ModelRole{inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode} {
		template := resolverTemplate()
		template.Role = role
		resolved, err := ResolveModelPool(template, profile)
		if err != nil {
			t.Fatal(err)
		}
		if resolved.PDRuntime == nil || resolved.PDRuntime.ProfileName != "cluster-pd" || resolved.PDRuntime.ProfileRevision != "release-A" || resolved.PDRuntime.Connector != "MooncakeConnector" {
			t.Fatalf("P/D runtime = %#v", resolved.PDRuntime)
		}
	}
}

func TestResolveModelPoolRejectsIncompleteOrNonSingleMooncakePD(t *testing.T) {
	template := resolverTemplate()
	template.Role = inferencev1alpha1.ModelRolePrefill
	if _, err := ResolveModelPool(template, resolverProfile()); err == nil {
		t.Fatal("P/D without profile was accepted")
	}
	profile := resolverProfile()
	profile.MooncakePD = &MooncakePDProfile{Name: "cluster-pd", Revision: "release-A", Protocol: "rdma", BootstrapPort: 29001, AbortRequestTimeoutSeconds: 30}
	template.Parallelism.TP = 2
	template.Resources.Requests.GPU.Count = 2
	if _, err := ResolveModelPool(template, profile); err == nil {
		t.Fatal("P/D multi-rank topology was accepted")
	}
}

func TestResolveModelPoolKVStoreAndRejectsPDOffload(t *testing.T) {
	profile := resolverProfile()
	profile.MooncakeStore = &MooncakeStoreProfile{Name: "store", Revision: "r1", ConfigMapName: "store-config", ConfigMapKey: "config.json", PythonHashSeed: "0"}
	template := resolverTemplate()
	template.KVCache = &inferencev1alpha1.NormalizedKVCache{MooncakeStore: &inferencev1alpha1.NormalizedMooncakeStore{Profile: "store"}}
	resolved, err := ResolveModelPool(template, profile)
	if err != nil || resolved.KVRuntime == nil || resolved.KVRuntime.MooncakeStore.ConfigMapName != "store-config" {
		t.Fatalf("KV store = %#v, err = %v", resolved.KVRuntime, err)
	}
	template.Role = inferencev1alpha1.ModelRolePrefill
	profile.MooncakePD = &MooncakePDProfile{Name: "pd", Revision: "r1", Protocol: "rdma", BootstrapPort: 29001, AbortRequestTimeoutSeconds: 1}
	resolved, err = ResolveModelPool(template, profile)
	if err != nil || resolved.PDRuntime == nil || resolved.KVRuntime == nil {
		t.Fatalf("P/D Store = %#v, err = %v", resolved, err)
	}
	template.KVCache = &inferencev1alpha1.NormalizedKVCache{Offload: &inferencev1alpha1.KVOffload{CPUCacheSize: "1Gi"}}
	if _, err := ResolveModelPool(template, profile); err == nil {
		t.Fatal("P/D local offload was accepted")
	}
}

func TestResolveModelPoolRejectsMissingStoreProfile(t *testing.T) {
	template := resolverTemplate()
	template.KVCache = &inferencev1alpha1.NormalizedKVCache{MooncakeStore: &inferencev1alpha1.NormalizedMooncakeStore{Profile: "missing"}}
	if _, err := ResolveModelPool(template, resolverProfile()); err == nil {
		t.Fatal("missing Store profile was accepted")
	}
}

func TestResolveModelPoolManagedKVBindingPinsUIDAndRevision(t *testing.T) {
	template := resolverTemplate()
	template.KVCache = &inferencev1alpha1.NormalizedKVCache{MooncakeStore: &inferencev1alpha1.NormalizedMooncakeStore{ManagedBinding: &inferencev1alpha1.ManagedMooncakeStoreBinding{Name: "cache", UID: "cache-uid", BindingRevision: "binding-r2", ConfigMapName: "cache-requester", ConfigMapKey: "mooncake.json", RequesterBufferBytes: 536870912}}}
	resolved, err := ResolveModelPool(template, resolverProfile())
	if err != nil {
		t.Fatal(err)
	}
	store := resolved.KVRuntime.MooncakeStore
	if store.ProfileName != "kvservice:cache" || store.KVServiceUID != "cache-uid" || store.ProfileRevision != "binding-r2" || store.ConfigMapName != "cache-requester" || store.ConfigMapKey != "mooncake.json" || store.PythonHashSeed != "0" || store.RequesterBufferBytes != 536870912 {
		t.Fatalf("managed Store runtime = %#v", store)
	}
	firstRevision := resolved.Revision
	template.KVCache.MooncakeStore.ManagedBinding.BindingRevision = "binding-r3"
	changed, err := ResolveModelPool(template, resolverProfile())
	if err != nil || changed.Revision == firstRevision {
		t.Fatalf("binding revision did not roll Group revision: %q -> %q, err = %v", firstRevision, changed.Revision, err)
	}
}

func TestResolveModelPoolRejectsManagedRequesterBufferOutsideBudget(t *testing.T) {
	template := resolverTemplate() // resolver fixture has a 1Gi memory request.
	template.KVCache = &inferencev1alpha1.NormalizedKVCache{MooncakeStore: &inferencev1alpha1.NormalizedMooncakeStore{ManagedBinding: &inferencev1alpha1.ManagedMooncakeStoreBinding{Name: "cache", UID: "cache-uid", BindingRevision: "r1", ConfigMapName: "requester", ConfigMapKey: "mooncake.json", RequesterBufferBytes: 4 * 1024 * 1024 * 1024}}}
	if _, err := ResolveModelPool(template, resolverProfile()); err == nil {
		t.Fatal("1Gi container request accepted a 4Gi managed requester buffer")
	}
}

func TestResolveModelPoolExternalProfileSkipsManagedBufferBudget(t *testing.T) {
	template := resolverTemplate()
	template.KVCache = &inferencev1alpha1.NormalizedKVCache{MooncakeStore: &inferencev1alpha1.NormalizedMooncakeStore{Profile: "store"}}
	profile := resolverProfile()
	profile.MooncakeStore = &MooncakeStoreProfile{Name: "store", Revision: "r1", ConfigMapName: "external", ConfigMapKey: "mooncake.json", PythonHashSeed: "0"}
	if _, err := ResolveModelPool(template, profile); err != nil {
		t.Fatalf("external Store profile must not require a managed buffer budget: %v", err)
	}
}

func TestResolveModelPoolMaterializesECProducerAndConsumer(t *testing.T) {
	profile := resolverProfile()
	profile.MooncakePD = &MooncakePDProfile{Name: "pd", Revision: "r1", Protocol: "rdma", BootstrapPort: 29001, AbortRequestTimeoutSeconds: 30}
	profile.EC = &ECProfile{Name: "verified-ec", Revision: "r2", Connector: "ECExampleConnector", RuntimeFingerprint: "vllm-pinned-ec-r2", SharedStorageClaim: "ec-rwx", SharedStoragePath: "/var/lib/foretoken/ec"}
	for role, want := range map[inferencev1alpha1.ModelRole]inferencev1alpha1.ECTransferRole{inferencev1alpha1.ModelRoleEncoder: inferencev1alpha1.ECTransferRoleProducer, inferencev1alpha1.ModelRolePrefill: inferencev1alpha1.ECTransferRoleConsumer} {
		template := resolverTemplate()
		template.Role, template.ECProfile = role, "verified-ec"
		resolved, err := ResolveModelPool(template, profile)
		if err != nil || resolved.ECRuntime == nil || resolved.ECRuntime.Role != want || resolved.ECRuntime.RuntimeFingerprint != "vllm-pinned-ec-r2" {
			t.Fatalf("%s EC runtime = %#v, err = %v", role, resolved.ECRuntime, err)
		}
	}
}
