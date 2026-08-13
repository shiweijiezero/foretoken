// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Tests deterministic ModelService intent compilation.

package compiler

import (
	"fmt"
	"reflect"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func TestCompileShorthand(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{
		Model:         "Qwen/Qwen3-0.6B",
		ModelRevision: "model-revision",
		Backend:       "vllm",
		Resources: &inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
			ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1000m", Memory: "1024Mi"},
			GPU:                     inferencev1alpha1.GPURequest{},
		}},
		Timeouts:       inferencev1alpha1.ModelTimeouts{Startup: "600s", Drain: "120s"},
		Parallelism:    &inferencev1alpha1.Parallelism{},
		MaxInputTokens: ptr(int32(16384)),
		ExtraArgs:      []inferencev1alpha1.BackendArg{"--max-model-len=32768"},
	}

	pools, err := CompileModelService(spec)
	if err != nil {
		t.Fatal(err)
	}
	if len(pools) != 1 || pools[0].Name != defaultPoolName || pools[0].DesiredGroups != 1 {
		t.Fatalf("compiled Pools = %#v", pools)
	}
	template := pools[0].Template
	if template.ModelRevision != "model-revision" {
		t.Fatalf("compiled artifacts = %#v", template)
	}
	if template.NodeCount != 1 || template.MemberCount != 1 || template.Role != inferencev1alpha1.ModelRoleAggregate {
		t.Fatalf("compiled template identity = %#v", template)
	}
	if template.Resources.Requests.CPU != "1" || template.Resources.Requests.Memory != "1Gi" {
		t.Fatalf("normalized resources = %#v", template.Resources)
	}
	if template.Resources.Requests.GPU.Type != "auto" || template.Resources.Requests.GPU.Count != 1 {
		t.Fatalf("normalized accelerator = %#v", template.Resources.Requests.GPU)
	}
	if template.Parallelism != (inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1}) {
		t.Fatalf("compiled parallelism = %#v", template.Parallelism)
	}
	if template.Timeouts.Startup != "10m0s" || template.Timeouts.Drain != "2m0s" {
		t.Fatalf("normalized timeouts = %#v", template.Timeouts)
	}
	if template.MaxInputTokens == nil || *template.MaxInputTokens != 16384 || template.MaxInputTokens == spec.MaxInputTokens {
		t.Fatalf("compiled maxInputTokens = %#v", template.MaxInputTokens)
	}
	if template.InternalGenerateRequestBodyLimitBytes != inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes {
		t.Fatalf("compiled internal generate request body limit = %d", template.InternalGenerateRequestBodyLimitBytes)
	}
}

func TestCompileAdvancedPools(t *testing.T) {
	zero := int32(0)
	two := int32(2)
	spec := inferencev1alpha1.ModelServiceSpec{
		Model:    "deepseek-ai/DeepSeek-V3",
		Backend:  "vllm",
		Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
		ModelPools: []inferencev1alpha1.ModelPoolTemplate{
			{
				Name:           "decode",
				Role:           inferencev1alpha1.ModelRoleDecode,
				Replicas:       &zero,
				Nodes:          &two,
				Resources:      modelResources("16", "64Gi", "nvidia-h100-80gb", 8),
				Parallelism:    inferencev1alpha1.Parallelism{EP: &inferencev1alpha1.ExpertParallelism{Size: 16}},
				MaxInputTokens: ptr(int32(8192)),
			},
			{
				Name:           "prefill",
				Role:           inferencev1alpha1.ModelRolePrefill,
				Replicas:       &two,
				Nodes:          &two,
				Resources:      modelResources("16", "64Gi", "nvidia-h100-80gb", 8),
				Parallelism:    inferencev1alpha1.Parallelism{TP: 8, PP: 2},
				MaxInputTokens: ptr(int32(32768)),
			},
		},
	}

	pools, err := CompileModelService(spec)
	if err != nil {
		t.Fatal(err)
	}
	if got := []string{pools[0].Name, pools[1].Name}; !reflect.DeepEqual(got, []string{"decode", "prefill"}) {
		t.Fatalf("Pool order = %v", got)
	}
	if pools[0].DesiredGroups != 0 || pools[0].Template.Parallelism.DP != 16 || pools[0].Template.Parallelism.EP == nil {
		t.Fatalf("compiled decode Pool = %#v", pools[0])
	}
	if pools[1].DesiredGroups != 2 || pools[1].Template.Parallelism.DP != 1 {
		t.Fatalf("compiled prefill Pool = %#v", pools[1])
	}
	if pools[0].Template.MaxInputTokens == nil || *pools[0].Template.MaxInputTokens != 8192 || pools[1].Template.MaxInputTokens == nil || *pools[1].Template.MaxInputTokens != 32768 {
		t.Fatalf("compiled advanced maxInputTokens = %#v", pools)
	}
}

func TestCompilePropagatesQuickStartFeatures(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{
		Model: "model", Backend: "vllm", Resources: ptr(modelResources("1", "1Gi", "auto", 1)),
		Parallelism: &inferencev1alpha1.Parallelism{}, Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"},
		Features: &inferencev1alpha1.ModelFeatures{Tools: true, Reasoning: true, StructuredOutputs: []inferencev1alpha1.StructuredOutputFormat{inferencev1alpha1.StructuredOutputFormatJSONSchema, inferencev1alpha1.StructuredOutputFormatJSONObject, inferencev1alpha1.StructuredOutputFormatJSONSchema}, Multimodal: []inferencev1alpha1.MultimodalModality{inferencev1alpha1.MultimodalModalityImage, inferencev1alpha1.MultimodalModalityImage}},
	}
	pools, err := CompileModelService(spec)
	if err != nil {
		t.Fatal(err)
	}
	got := pools[0].Template.Features
	if !got.Tools || !got.Reasoning || !reflect.DeepEqual(got.StructuredOutputs, []inferencev1alpha1.StructuredOutputFormat{inferencev1alpha1.StructuredOutputFormatJSONObject, inferencev1alpha1.StructuredOutputFormatJSONSchema}) || !reflect.DeepEqual(got.Multimodal, []inferencev1alpha1.MultimodalModality{inferencev1alpha1.MultimodalModalityImage}) {
		t.Fatalf("normalized quick-start features = %#v", got)
	}
}

func TestCompileRejectsUnsupportedMultimodalFeatures(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{Model: "model", Backend: "vllm", Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"}, ModelPools: []inferencev1alpha1.ModelPoolTemplate{{Name: "aggregate", Resources: modelResources("1", "1Gi", "auto", 1), Parallelism: inferencev1alpha1.Parallelism{}, Features: &inferencev1alpha1.ModelFeatures{Multimodal: []inferencev1alpha1.MultimodalModality{"audio"}}}}}
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("unsupported audio modality was accepted")
	}
}

func TestCompilePropagatesPDMultiModalModelFeatures(t *testing.T) {
	features := &inferencev1alpha1.ModelFeatures{Multimodal: []inferencev1alpha1.MultimodalModality{inferencev1alpha1.MultimodalModalityImage}}
	spec := inferencev1alpha1.ModelServiceSpec{Model: "model", Backend: "vllm", Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"}, ModelPools: []inferencev1alpha1.ModelPoolTemplate{{Name: "prefill", Role: inferencev1alpha1.ModelRolePrefill, Resources: modelResources("1", "1Gi", "auto", 1), Parallelism: inferencev1alpha1.Parallelism{}, Features: features}, {Name: "decode", Role: inferencev1alpha1.ModelRoleDecode, Resources: modelResources("1", "1Gi", "auto", 1), Parallelism: inferencev1alpha1.Parallelism{}, Features: features}}}
	pools, err := CompileModelService(spec)
	if err != nil {
		t.Fatal(err)
	}
	for _, pool := range pools {
		if !reflect.DeepEqual(pool.Template.Features.Multimodal, features.Multimodal) {
			t.Fatalf("%s multimodal features = %#v", pool.Name, pool.Template.Features.Multimodal)
		}
	}
}

func TestCompileRejectsIncompleteOrMixedPDRoles(t *testing.T) {
	base := inferencev1alpha1.ModelPoolTemplate{
		Name:        "pool",
		Resources:   modelResources("1", "1Gi", "auto", 1),
		Parallelism: inferencev1alpha1.Parallelism{},
	}
	for name, roles := range map[string][]inferencev1alpha1.ModelRole{
		"prefill only":    {inferencev1alpha1.ModelRolePrefill},
		"decode only":     {inferencev1alpha1.ModelRoleDecode},
		"mixed aggregate": {inferencev1alpha1.ModelRoleAggregate, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode},
	} {
		t.Run(name, func(t *testing.T) {
			spec := inferencev1alpha1.ModelServiceSpec{
				Model:    "model",
				Backend:  "vllm",
				Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"},
			}
			for index, role := range roles {
				pool := base
				pool.Name = fmt.Sprintf("pool-%d", index)
				pool.Role = role
				spec.ModelPools = append(spec.ModelPools, pool)
			}
			if _, err := CompileModelService(spec); err == nil {
				t.Fatalf("roles %v were accepted", roles)
			}
		})
	}
}

func TestCompileValidatesAutoscalingPolicy(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{
		Model: "model", Backend: "vllm", Resources: ptr(modelResources("1", "1Gi", "auto", 1)),
		Parallelism: &inferencev1alpha1.Parallelism{}, Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"},
		Autoscaling: &inferencev1alpha1.ModelAutoscalingConfig{Algorithm: inferencev1alpha1.AutoscalingAlgorithmQueue},
	}
	minimum, maximum := int32(1), int32(2)
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("automatic autoscaling without maxGroups was accepted")
	}
	spec.Autoscaling.Algorithm = inferencev1alpha1.AutoscalingAlgorithmThreshold
	spec.Autoscaling.MinGroups = &minimum
	spec.Autoscaling.MaxGroups = &maximum
	negativeThreshold := int64(-1)
	spec.Autoscaling.ScaleUpQueue = &negativeThreshold
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("negative threshold scaleUpQueue was accepted")
	}
	spec.Autoscaling.ScaleUpQueue = nil
	spec.Autoscaling.Trigger = &inferencev1alpha1.ModelAutoscalingTriggerConfig{Interval: "invalid"}
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("invalid autoscaling poll interval was accepted")
	}
	spec.Autoscaling.Trigger.Interval = "5s"
	if _, err := CompileModelService(spec); err != nil {
		t.Fatalf("valid threshold autoscaling config was rejected: %v", err)
	}
	spec.Autoscaling.Trigger = &inferencev1alpha1.ModelAutoscalingTriggerConfig{Algorithm: inferencev1alpha1.AutoscalingTriggerAlgorithmWatermark, LowQueuePerRoutableGroup: ptr(int64(0))}
	low, high := int64(2), int64(1)
	spec.Autoscaling.Trigger.LowQueuePerRoutableGroup = &low
	spec.Autoscaling.Trigger.HighQueuePerRoutableGroup = &high
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("inverted watermark bounds were accepted")
	}
}

func TestCompileRejectsCapacityMismatch(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{
		Model:       "Qwen/Qwen3-0.6B",
		Backend:     "vllm",
		Resources:   ptr(modelResources("1", "1Gi", "auto", 1)),
		Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
		Parallelism: &inferencev1alpha1.Parallelism{TP: 2},
	}
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("capacity mismatch was accepted")
	}
}

func modelResources(cpu, memory, accelerator string, count int32) inferencev1alpha1.ModelResources {
	return inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
		ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: inferencev1alpha1.ResourceQuantity(cpu), Memory: inferencev1alpha1.ResourceQuantity(memory)},
		GPU:                     inferencev1alpha1.GPURequest{Type: accelerator, Count: count},
	}}
}

func ptr[T any](value T) *T { return &value }

func TestCompileNormalizesKVOffload(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{Model: "model", Backend: "vllm", Resources: &inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}, GPU: inferencev1alpha1.GPURequest{}}}, Parallelism: &inferencev1alpha1.Parallelism{}, Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"}, KVCache: &inferencev1alpha1.KVCache{Offload: &inferencev1alpha1.KVOffload{CPUCacheSize: "1024Mi", Filesystem: true}}}
	pools, err := CompileModelService(spec)
	if err != nil || pools[0].Template.KVCache.Offload.CPUCacheSize != "1Gi" {
		t.Fatalf("KV cache = %#v, err = %v", pools, err)
	}
}

func TestCompileRejectsConflictingKVCache(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{Model: "model", Backend: "vllm", Resources: &inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}, GPU: inferencev1alpha1.GPURequest{}}}, Parallelism: &inferencev1alpha1.Parallelism{}, Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"}, KVCache: &inferencev1alpha1.KVCache{Offload: &inferencev1alpha1.KVOffload{CPUCacheSize: "1Gi"}, MooncakeStore: &inferencev1alpha1.MooncakeStore{Profile: "store"}}}
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("conflicting KV cache was accepted")
	}
}

func TestCompileRejectsFractionalKVOffloadBytes(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{Model: "model", Backend: "vllm", Resources: &inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}, GPU: inferencev1alpha1.GPURequest{}}}, Parallelism: &inferencev1alpha1.Parallelism{}, Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"}, KVCache: &inferencev1alpha1.KVCache{Offload: &inferencev1alpha1.KVOffload{CPUCacheSize: "400m"}}}
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("fractional KV offload quantity was accepted")
	}
}

func TestCompileEPDPropagatesECProfileAndRejectsIncompleteTriplets(t *testing.T) {
	base := func(name string, role inferencev1alpha1.ModelRole) inferencev1alpha1.ModelPoolTemplate {
		return inferencev1alpha1.ModelPoolTemplate{Name: name, Role: role, Resources: modelResources("1", "1Gi", "auto", 1), Parallelism: inferencev1alpha1.Parallelism{}}
	}
	spec := inferencev1alpha1.ModelServiceSpec{Model: "model", Backend: "vllm", Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"}, ECProfile: &inferencev1alpha1.ECProfileReference{Profile: "verified-ec"}, ModelPools: []inferencev1alpha1.ModelPoolTemplate{base("encoder", inferencev1alpha1.ModelRoleEncoder), base("prefill", inferencev1alpha1.ModelRolePrefill), base("decode", inferencev1alpha1.ModelRoleDecode)}}
	pools, err := CompileModelService(spec)
	if err != nil {
		t.Fatal(err)
	}
	for _, pool := range pools {
		if (pool.Template.Role == inferencev1alpha1.ModelRoleEncoder || pool.Template.Role == inferencev1alpha1.ModelRolePrefill) && pool.Template.ECProfile != "verified-ec" {
			t.Fatalf("EC profile not propagated: %#v", pool.Template)
		}
		if pool.Template.Role == inferencev1alpha1.ModelRoleDecode && pool.Template.ECProfile != "" {
			t.Fatalf("decode received EC profile: %#v", pool.Template)
		}
	}
	spec.ModelPools = spec.ModelPools[:2]
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("incomplete E/P/D triplet was accepted")
	}

	spec.ModelPools = []inferencev1alpha1.ModelPoolTemplate{
		base("encoder-a", inferencev1alpha1.ModelRoleEncoder),
		base("encoder-b", inferencev1alpha1.ModelRoleEncoder),
		base("prefill", inferencev1alpha1.ModelRolePrefill),
		base("decode", inferencev1alpha1.ModelRoleDecode),
	}
	if _, err := CompileModelService(spec); err == nil {
		t.Fatal("multiple encoder Pools were accepted for E/P/D")
	}
}

func TestCompileInternalGenerateRequestBodyLimit(t *testing.T) {
	limit := int64(96 * 1024 * 1024)
	spec := inferencev1alpha1.ModelServiceSpec{
		Model: "model", Backend: "vllm", Resources: ptr(modelResources("1", "1Gi", "auto", 1)),
		Parallelism: &inferencev1alpha1.Parallelism{}, Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "1m", Drain: "1m"},
		InternalGenerateRequestBodyLimitBytes: &limit,
	}
	pools, err := CompileModelService(spec)
	if err != nil || pools[0].Template.InternalGenerateRequestBodyLimitBytes != limit {
		t.Fatalf("compiled internal generate request body limit = %#v, err = %v", pools, err)
	}
	for _, invalid := range []int64{inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes - 1, inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes + 1} {
		spec.InternalGenerateRequestBodyLimitBytes = &invalid
		if _, err := CompileModelService(spec); err == nil {
			t.Fatalf("invalid limit %d was accepted", invalid)
		}
	}
}
