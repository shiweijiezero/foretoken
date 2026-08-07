// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package vllm

import (
	"strings"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func TestCompilePreservesOnlyAllowedExtraArgs(t *testing.T) {
	template := testVLLMTemplate(1)
	template.ExtraArgs = []inferencev1alpha1.BackendArg{"--max-model-len=32768", "--enforce-eager"}
	config, err := Compile(template)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := config.ExtraArgs, template.ExtraArgs; len(got) != len(want) || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("extraArgs = %#v", got)
	}
}

func TestCompileRejectsExtraArgBypass(t *testing.T) {
	for _, argument := range []inferencev1alpha1.BackendArg{
		"--model=other", "--tensor-parallel-size=2", "-dp=2", "--data_parallel_size=2",
		"--max-model-len 32768", "--config=config.yaml", "--max-model-len=1=2", "--unknown=1",
	} {
		template := testVLLMTemplate(1)
		template.ExtraArgs = append(template.ExtraArgs, argument)
		if _, err := Compile(template); err == nil {
			t.Fatalf("%q was accepted", argument)
		}
	}
}

func TestCompileRejectsDuplicateExtraArgs(t *testing.T) {
	template := testVLLMTemplate(1)
	template.ExtraArgs = []inferencev1alpha1.BackendArg{"--max-model-len=1", "--max-model-len=2"}
	if _, err := Compile(template); err == nil {
		t.Fatal("duplicate extraArg was accepted")
	}
}

func TestBuildLaunchPlanKVVariants(t *testing.T) {
	base := testGroup()
	cases := []struct {
		name, kind, role string
		mutate           func(*inferencev1alpha1.ModelGroupSpec)
	}{
		{"aggregate", kvNone, "", func(*inferencev1alpha1.ModelGroupSpec) {}},
		{"pd", kvPD, "kv_consumer", func(g *inferencev1alpha1.ModelGroupSpec) {
			g.Role = inferencev1alpha1.ModelRoleDecode
			g.PDRuntime = &inferencev1alpha1.ModelGroupPDRuntimeConfig{Protocol: "rdma"}
		}},
		{"cpu", kvCPUOffload, "", func(g *inferencev1alpha1.ModelGroupSpec) {
			g.KVRuntime = &inferencev1alpha1.ModelGroupKVRuntimeConfig{Offload: &inferencev1alpha1.ModelGroupKVOffloadRuntime{CPUBytes: 1024}}
		}},
		{"filesystem", kvFilesystemOffload, "", func(g *inferencev1alpha1.ModelGroupSpec) {
			g.KVRuntime = &inferencev1alpha1.ModelGroupKVRuntimeConfig{Offload: &inferencev1alpha1.ModelGroupKVOffloadRuntime{CPUBytes: 1024, Filesystem: true}}
		}},
		{"store", kvMooncakeStore, "kv_both", func(g *inferencev1alpha1.ModelGroupSpec) { g.KVRuntime = storeRuntime() }},
		{"multi", kvMultiConnector, "kv_producer", func(g *inferencev1alpha1.ModelGroupSpec) {
			g.Role = inferencev1alpha1.ModelRolePrefill
			g.PDRuntime = &inferencev1alpha1.ModelGroupPDRuntimeConfig{Protocol: "rdma"}
			g.KVRuntime = storeRuntime()
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			group := base
			tc.mutate(&group)
			plan, err := BuildLaunchPlan(group)
			if err != nil {
				t.Fatal(err)
			}
			if plan.KV.Kind != tc.kind || plan.KV.Role != tc.role || !plan.KV.Events {
				t.Fatalf("kv = %#v", plan.KV)
			}
			json, err := plan.CanonicalJSON()
			if err != nil || !strings.Contains(json, `"version":1`) || !strings.Contains(json, `"kind":"`+tc.kind+`"`) {
				t.Fatalf("json = %q, %v", json, err)
			}
		})
	}
}

func TestBuildLaunchPlanRejectsInvalidTopology(t *testing.T) {
	group := testGroup()
	group.Parallelism.PCP = 2
	group.Parallelism.DP = 2
	if _, err := BuildLaunchPlan(group); err == nil {
		t.Fatal("invalid PCP/DP was accepted")
	}
}

func testVLLMTemplate(gpus int32) inferencev1alpha1.NormalizedPoolTemplate {
	return inferencev1alpha1.NormalizedPoolTemplate{Model: "Qwen/Qwen3-0.6B", ModelRevision: "main", Tokenizer: "Qwen/Qwen3-0.6B", TokenizerRevision: "main", Backend: "vllm", Role: inferencev1alpha1.ModelRoleAggregate, NodeCount: 1, MemberCount: 1, Resources: inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}, GPU: inferencev1alpha1.GPURequest{Type: "auto", Count: gpus}}}, Parallelism: inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1}, Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"}}
}

func testGroup() inferencev1alpha1.ModelGroupSpec {
	return inferencev1alpha1.ModelGroupSpec{Role: inferencev1alpha1.ModelRoleAggregate, Artifacts: inferencev1alpha1.ModelGroupArtifacts{Model: "model", ModelRevision: "revision", Tokenizer: "tokenizer", TokenizerRevision: "tokenizer-revision"}, Runtime: inferencev1alpha1.ModelGroupRuntime{Args: []inferencev1alpha1.BackendArg{"--max-model-len=32768"}}, Parallelism: inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1}, Timeouts: inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"}}
}

func storeRuntime() *inferencev1alpha1.ModelGroupKVRuntimeConfig {
	return &inferencev1alpha1.ModelGroupKVRuntimeConfig{MooncakeStore: &inferencev1alpha1.ModelGroupMooncakeStoreRuntime{ProfileName: "store", ProfileRevision: "r", ConfigMapName: "cm", ConfigMapKey: "k", PythonHashSeed: "0"}}
}

func TestBuildLaunchPlanProjectsOnlyTypedECRuntime(t *testing.T) {
	group := testGroup()
	group.Role = inferencev1alpha1.ModelRoleEncoder
	group.ECRuntime = &inferencev1alpha1.ModelGroupECRuntimeConfig{ProfileName: "verified-ec", ProfileRevision: "r2", Connector: "ECExampleConnector", Role: inferencev1alpha1.ECTransferRoleProducer, RuntimeFingerprint: "vllm-pinned-ec-r2", SharedStorageClaim: "ec-rwx", SharedStoragePath: "/var/lib/foretoken/ec"}
	plan, err := BuildLaunchPlan(group)
	if err != nil || plan.EC == nil || plan.EC.Role != "producer" || plan.EC.Connector != "ECExampleConnector" {
		t.Fatalf("EC launch plan = %#v, err = %v", plan.EC, err)
	}
	group.ECRuntime.Connector = "arbitrary.module"
	if _, err := BuildLaunchPlan(group); err == nil {
		t.Fatal("arbitrary EC connector was accepted")
	}
}
