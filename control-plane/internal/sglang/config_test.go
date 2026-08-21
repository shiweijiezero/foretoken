// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package sglang

import (
	"encoding/json"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

func TestCompileAcceptsAggregateTemplate(t *testing.T) {
	config, err := Compile(testTemplate())
	if err != nil {
		t.Fatalf("valid aggregate template was rejected: %v", err)
	}
	if config.Model != "model" || config.Revision != "main" || config.TP != 1 || config.DP != 2 {
		t.Fatalf("unexpected effective config: %#v", config)
	}
}

func TestCompileRejectsUnsupportedTopologies(t *testing.T) {
	for name, mutate := range map[string]func(*inferencev1alpha1.NormalizedPoolTemplate){
		"non-sglang backend": func(template *inferencev1alpha1.NormalizedPoolTemplate) { template.Backend = "vllm" },
		"non-aggregate role": func(template *inferencev1alpha1.NormalizedPoolTemplate) {
			template.Role = inferencev1alpha1.ModelRoleDecode
		},
		"pipeline parallel": func(template *inferencev1alpha1.NormalizedPoolTemplate) { template.Parallelism.PP = 2 },
		"expert parallel": func(template *inferencev1alpha1.NormalizedPoolTemplate) {
			template.Parallelism.EP = &inferencev1alpha1.ExpertParallelism{Size: 2}
		},
		"capacity mismatch": func(template *inferencev1alpha1.NormalizedPoolTemplate) {
			template.Parallelism.DP = 3
		},
	} {
		t.Run(name, func(t *testing.T) {
			template := testTemplate()
			mutate(&template)
			if _, err := Compile(template); err == nil {
				t.Fatalf("unsupported template %q was accepted", name)
			}
		})
	}
}

func TestCompileExtraArgsBoundary(t *testing.T) {
	template := testTemplate()
	template.ExtraArgs = []inferencev1alpha1.BackendArg{"--max-total-tokens=8192", "--disable-radix-cache"}
	if config, err := Compile(template); err != nil || len(config.ExtraArgs) != 2 {
		t.Fatalf("valid extraArgs = %#v, err = %v", config.ExtraArgs, err)
	}
	for _, args := range [][]inferencev1alpha1.BackendArg{
		{"--model=other"},
		{"--max-total-tokens 8192"},
		{"--unknown=1"},
		{"--max-total-tokens=1", "--max-total-tokens=2"},
		{"--disable-radix-cache=1"},
		{"--max-total-tokens"},
	} {
		template.ExtraArgs = args
		if _, err := Compile(template); err == nil {
			t.Fatalf("unsafe extraArgs %v were accepted", args)
		}
	}
}

func TestBuildLaunchPlanJSONContract(t *testing.T) {
	plan, err := BuildLaunchPlan(testGroup())
	if err != nil {
		t.Fatal(err)
	}
	jsonText, err := plan.JSON()
	if err != nil {
		t.Fatal(err)
	}
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal([]byte(jsonText), &decoded); err != nil {
		t.Fatalf("launch plan is not valid JSON: %v", err)
	}
	for _, field := range []string{
		"version", "model", "tp", "dp", "port", "startupSeconds", "drainSeconds",
		"extraArgs", "internalGenerateRequestBodyLimitBytes",
	} {
		if _, ok := decoded[field]; !ok {
			t.Fatalf("launch plan JSON is missing field %q: %s", field, jsonText)
		}
	}
	// The contract is flat: no nested lifecycle, no vLLM-only fields.
	for _, absent := range []string{"lifecycle", "kind", "nodeCount", "artifacts", "parallelism", "kv", "ec"} {
		if _, ok := decoded[absent]; ok {
			t.Fatalf("launch plan JSON must not contain field %q: %s", absent, jsonText)
		}
	}
	if plan.Port != 30000 {
		t.Fatalf("loopback port = %d, want 30000", plan.Port)
	}
	if plan.StartupSeconds != 600 || plan.DrainSeconds != 120 {
		t.Fatalf("lifecycle seconds = %d/%d, want 600/120", plan.StartupSeconds, plan.DrainSeconds)
	}
}

func TestBuildLaunchPlanOmitsEmptyRevision(t *testing.T) {
	group := testGroup()
	group.Artifacts.ModelRevision = ""
	plan, err := BuildLaunchPlan(group)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := planRevision(plan); ok {
		t.Fatal("empty revision must be omitted from the launch plan")
	}
}

func TestBuildLaunchPlanRejectsStartupTimeoutOverflow(t *testing.T) {
	group := testGroup()
	group.Timeouts.Startup = "2147483648s"
	if _, err := BuildLaunchPlan(group); err == nil {
		t.Fatal("startup timeout that overflows Deployment progressDeadlineSeconds was accepted")
	}
}

func TestBuildLaunchPlanRejectsNonAggregateRole(t *testing.T) {
	group := testGroup()
	group.Role = inferencev1alpha1.ModelRolePrefill
	if _, err := BuildLaunchPlan(group); err == nil {
		t.Fatal("non-aggregate SGLang group was accepted")
	}
}

func planRevision(plan SglangLaunchPlanV1) (string, bool) {
	raw, err := json.Marshal(plan)
	if err != nil {
		return "", true
	}
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal(raw, &decoded); err != nil {
		return "", true
	}
	revision, ok := decoded["revision"]
	return string(revision), ok
}

func testTemplate() inferencev1alpha1.NormalizedPoolTemplate {
	return inferencev1alpha1.NormalizedPoolTemplate{
		Model: "model", ModelRevision: "main",
		Backend: "sglang", Role: inferencev1alpha1.ModelRoleAggregate, NodeCount: 1, MemberCount: 1,
		Resources:                             inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{GPU: inferencev1alpha1.GPURequest{Count: 2}}},
		Parallelism:                           inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 2, PCP: 1, DCP: 1},
		Timeouts:                              inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
		InternalGenerateRequestBodyLimitBytes: inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes,
	}
}

func testGroup() inferencev1alpha1.ModelGroupSpec {
	return inferencev1alpha1.ModelGroupSpec{
		Role: inferencev1alpha1.ModelRoleAggregate, NodeCount: 1, MemberCount: 1,
		Artifacts:   inferencev1alpha1.ModelGroupArtifacts{Model: "model", ModelRevision: "main"},
		Runtime:     inferencev1alpha1.ModelGroupRuntime{InternalGenerateRequestBodyLimitBytes: inferencev1alpha1.DefaultInternalGenerateRequestBodyLimitBytes},
		Parallelism: inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 2, PCP: 1, DCP: 1},
		Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
	}
}
