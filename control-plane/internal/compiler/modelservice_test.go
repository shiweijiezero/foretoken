// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Tests deterministic ModelService intent compilation.

package compiler

import (
	"reflect"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func TestCompileShorthand(t *testing.T) {
	spec := inferencev1alpha1.ModelServiceSpec{
		Model:   "Qwen/Qwen3-0.6B",
		Backend: "vllm",
		Resources: &inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
			ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1000m", Memory: "1024Mi"},
			GPU:                     inferencev1alpha1.GPURequest{},
		}},
		Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "600s", Drain: "120s"},
		Parallelism: &inferencev1alpha1.Parallelism{},
		ExtraArgs:   []inferencev1alpha1.BackendArg{"--max-model-len=32768"},
	}

	pools, err := CompileModelService(spec)
	if err != nil {
		t.Fatal(err)
	}
	if len(pools) != 1 || pools[0].Name != defaultPoolName || pools[0].DesiredGroups != 1 {
		t.Fatalf("compiled Pools = %#v", pools)
	}
	template := pools[0].Template
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
				Name:        "decode",
				Role:        inferencev1alpha1.ModelRoleDecode,
				Replicas:    &zero,
				Nodes:       &two,
				Resources:   modelResources("16", "64Gi", "nvidia-h100-80gb", 8),
				Parallelism: inferencev1alpha1.Parallelism{EP: &inferencev1alpha1.ExpertParallelism{Size: 16}},
			},
			{
				Name:        "prefill",
				Role:        inferencev1alpha1.ModelRolePrefill,
				Replicas:    &two,
				Nodes:       &two,
				Resources:   modelResources("16", "64Gi", "nvidia-h100-80gb", 8),
				Parallelism: inferencev1alpha1.Parallelism{TP: 8, PP: 2},
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
