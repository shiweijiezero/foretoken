// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package resolver

import (
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func TestResolveModelPoolSglang(t *testing.T) {
	profile := testProfile()
	resolved, err := ResolveModelPool(testSglangTemplate(), profile)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Runtime.Backend != "sglang" {
		t.Fatalf("backend = %q, want sglang", resolved.Runtime.Backend)
	}
	if resolved.Runtime.Image != profile.SglangImage {
		t.Fatalf("image = %q, want %q", resolved.Runtime.Image, profile.SglangImage)
	}
	if resolved.PDRuntime != nil || resolved.ECRuntime != nil || resolved.KVRuntime != nil {
		t.Fatalf("SGLang must not resolve P/D/EC/KV runtimes: %#v", resolved)
	}
	if resolved.Artifacts.Model != "model" || resolved.Artifacts.ModelRevision != "main" {
		t.Fatalf("artifacts = %#v", resolved.Artifacts)
	}
	if resolved.Parallelism.TP != 1 || resolved.Parallelism.DP != 2 || resolved.Parallelism.PP != 1 {
		t.Fatalf("parallelism = %#v", resolved.Parallelism)
	}
}

func TestResolveModelPoolSglangRequiresImage(t *testing.T) {
	profile := testProfile()
	profile.SglangImage = ""
	if _, err := ResolveModelPool(testSglangTemplate(), profile); err == nil {
		t.Fatal("SGLang pool without an image was accepted")
	}
}

func TestResolveModelPoolVllmRegression(t *testing.T) {
	profile := testProfile()
	resolved, err := ResolveModelPool(testVllmTemplate(), profile)
	if err != nil {
		t.Fatal(err)
	}
	if resolved.Runtime.Backend != "vllm" || resolved.Runtime.Image != profile.VllmImage {
		t.Fatalf("vllm runtime = %#v", resolved.Runtime)
	}
}

func testProfile() RuntimeProfile {
	return RuntimeProfile{
		Revision:           "default",
		VllmImage:          "vllm-image:latest",
		SglangImage:        "sglang-image:latest",
		ModelServerPort:    9000,
		DeviceResourceName: "nvidia.com/gpu",
	}
}

func testSglangTemplate() inferencev1alpha1.NormalizedPoolTemplate {
	return inferencev1alpha1.NormalizedPoolTemplate{
		Model: "model", ModelRevision: "main",
		Backend: "sglang", Role: inferencev1alpha1.ModelRoleAggregate, NodeCount: 1, MemberCount: 1,
		Resources:                             inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{GPU: inferencev1alpha1.GPURequest{Count: 2}}},
		Parallelism:                           inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 2, PCP: 1, DCP: 1},
		Timeouts:                              inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
		InternalGenerateRequestBodyLimitBytes: inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes,
	}
}

func testVllmTemplate() inferencev1alpha1.NormalizedPoolTemplate {
	return inferencev1alpha1.NormalizedPoolTemplate{
		Model: "model", ModelRevision: "main", Tokenizer: "model", TokenizerRevision: "main",
		Backend: "vllm", Role: inferencev1alpha1.ModelRoleAggregate, NodeCount: 1, MemberCount: 1,
		Resources:                             inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{GPU: inferencev1alpha1.GPURequest{Count: 1}}},
		Parallelism:                           inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1},
		Timeouts:                              inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
		InternalGenerateRequestBodyLimitBytes: inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes,
	}
}
