// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package vllm

import (
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// TestCompileExtraArgsBoundary protects controller-owned vLLM arguments from user extraArgs overrides.
func TestCompileExtraArgsBoundary(t *testing.T) {
	template := testVLLMTemplate(1)
	template.ExtraArgs = []inferencev1alpha1.BackendArg{"--max-model-len=32768", "--enforce-eager"}
	if config, err := Compile(template); err != nil || len(config.ExtraArgs) != 2 {
		t.Fatalf("valid extraArgs = %#v, err = %v", config.ExtraArgs, err)
	}
	for _, args := range [][]inferencev1alpha1.BackendArg{
		{"--model=other"},
		{"--max-model-len 32768"},
		{"--unknown=1"},
		{"--max-model-len=1", "--max-model-len=2"},
	} {
		template.ExtraArgs = args
		if _, err := Compile(template); err == nil {
			t.Fatalf("unsafe extraArgs %v were accepted", args)
		}
	}
}

// TestBuildLaunchPlanRuntimeVariants protects the Go producer shape for aggregate, P/D, E/P/D, KV, and EC launch variants.
func TestBuildLaunchPlanRuntimeVariants(t *testing.T) {
	cases := []struct {
		name, kind, role string
		mutate           func(*inferencev1alpha1.ModelGroupSpec)
	}{
		{"aggregate", kvNone, "", func(*inferencev1alpha1.ModelGroupSpec) {}},
		{"pd", kvPD, "kv_consumer", func(group *inferencev1alpha1.ModelGroupSpec) {
			group.Role = inferencev1alpha1.ModelRoleDecode
			group.PDRuntime = &inferencev1alpha1.ModelGroupPDRuntimeConfig{Protocol: "rdma", RDMADeviceName: "mlx5_1"}
		}},
		{"cpu offload", kvCPUOffload, "", func(group *inferencev1alpha1.ModelGroupSpec) {
			group.KVRuntime = &inferencev1alpha1.ModelGroupKVRuntimeConfig{Offload: &inferencev1alpha1.ModelGroupKVOffloadRuntime{CPUBytes: 1024}}
		}},
		{"filesystem offload", kvFilesystemOffload, "", func(group *inferencev1alpha1.ModelGroupSpec) {
			group.KVRuntime = &inferencev1alpha1.ModelGroupKVRuntimeConfig{Offload: &inferencev1alpha1.ModelGroupKVOffloadRuntime{CPUBytes: 1024, Filesystem: true}}
		}},
		{"store", kvMooncakeStore, "kv_both", func(group *inferencev1alpha1.ModelGroupSpec) { group.KVRuntime = storeRuntime() }},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			group := testGroup()
			test.mutate(&group)
			plan, err := BuildLaunchPlan(group)
			if err != nil {
				t.Fatal(err)
			}
			if plan.KV.Kind != test.kind || plan.KV.Role != test.role {
				t.Fatalf("KV plan = %#v", plan.KV)
			}
		})
	}

	group := testGroup()
	group.Role = inferencev1alpha1.ModelRoleEncoder
	group.ECRuntime = &inferencev1alpha1.ModelGroupECRuntimeConfig{ProfileName: "ec", ProfileRevision: "r1", Connector: "ECExampleConnector", Role: inferencev1alpha1.ECTransferRoleProducer, SharedStorageClaim: "shared", SharedStoragePath: "/shared"}
	plan, err := BuildLaunchPlan(group)
	if err != nil || plan.EC == nil || plan.EC.Role != "producer" {
		t.Fatalf("EC plan = %#v, err = %v", plan.EC, err)
	}
}

// TestBuildLaunchPlanRejectsStartupTimeoutOverflow protects Kubernetes progress-deadline conversion from integer overflow.
func TestBuildLaunchPlanRejectsStartupTimeoutOverflow(t *testing.T) {
	group := testGroup()
	group.Timeouts.Startup = "2147483648s"
	if _, err := BuildLaunchPlan(group); err == nil {
		t.Fatal("startup timeout that overflows Deployment progressDeadlineSeconds was accepted")
	}
}

func testVLLMTemplate(gpus int32) inferencev1alpha1.NormalizedPoolTemplate {
	return inferencev1alpha1.NormalizedPoolTemplate{
		Model: "model", ModelRevision: "main", Tokenizer: "model", TokenizerRevision: "main",
		Backend: "vllm", Role: inferencev1alpha1.ModelRoleAggregate, NodeCount: 1, MemberCount: 1,
		Resources:                             inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{GPU: inferencev1alpha1.GPURequest{Count: gpus}}},
		Parallelism:                           inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1},
		Timeouts:                              inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
		InternalGenerateRequestBodyLimitBytes: inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes,
	}
}

func testGroup() inferencev1alpha1.ModelGroupSpec {
	return inferencev1alpha1.ModelGroupSpec{
		Role: inferencev1alpha1.ModelRoleAggregate, NodeCount: 1, MemberCount: 1,
		Artifacts:   inferencev1alpha1.ModelGroupArtifacts{Model: "model", ModelRevision: "main", Tokenizer: "model", TokenizerRevision: "main"},
		Runtime:     inferencev1alpha1.ModelGroupRuntime{InternalGenerateRequestBodyLimitBytes: inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes},
		Parallelism: inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1},
		Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
	}
}

func storeRuntime() *inferencev1alpha1.ModelGroupKVRuntimeConfig {
	return &inferencev1alpha1.ModelGroupKVRuntimeConfig{MooncakeStore: &inferencev1alpha1.ModelGroupMooncakeStoreRuntime{ProfileName: "store", ProfileRevision: "r1", ConfigMapName: "config", ConfigMapKey: "mooncake.json", PythonHashSeed: "0"}}
}
