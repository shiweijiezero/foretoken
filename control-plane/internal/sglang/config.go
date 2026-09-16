// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compiles validated ModelPool templates and ModelGroup contracts for SGLang.

package sglang

import (
	"encoding/json"
	"fmt"
	"math"
	"strings"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// sglangLoopbackPort is the fixed loopback HTTP port for the SGLang server child,
// which shares the Pod with the Foretoken model-server. A single member Pod hosts
// exactly one engine, so the loopback port never collides across Pods.
const sglangLoopbackPort = 30000

const maxKubernetesInt32Seconds = int64(1<<31 - 1)

// SglangLaunchPlanV1 is the versioned, private Go-to-Rust launch contract. Rust is
// the only component that renders this contract into SGLang command-line flags.
type SglangLaunchPlanV1 struct {
	Version                               int      `json:"version"`
	Model                                 string   `json:"model"`
	Revision                              string   `json:"revision,omitempty"`
	TP                                    int32    `json:"tp"`
	DP                                    int32    `json:"dp"`
	Port                                  int32    `json:"port"`
	StartupSeconds                        int64    `json:"startupSeconds"`
	DrainSeconds                          int64    `json:"drainSeconds"`
	ExtraArgs                             []string `json:"extraArgs"`
	InternalGenerateRequestBodyLimitBytes int64    `json:"internalGenerateRequestBodyLimitBytes"`
}

// sglangValueArgs and sglangBoolArgs define the controller-owned extraArgs policy.
var sglangValueArgs = map[string]bool{
	"--max-total-tokens":     true,
	"--context-length":       true,
	"--chunked-prefill-size": true,
	"--schedule-policy":      true,
	"--attention-backend":    true,
}

var sglangBoolArgs = map[string]bool{
	"--disable-radix-cache":  true,
	"--enable-torch-compile": true,
}

// Validate checks whether a normalized Pool template can run on SGLang.
func Validate(template inferencev1alpha1.NormalizedPoolTemplate) error {
	if template.Backend != "sglang" {
		return fmt.Errorf("SGLang validation requires backend sglang")
	}
	if err := validateTopology(template.NodeCount, template.MemberCount, template.Role, template.Parallelism); err != nil {
		return err
	}
	if err := validateExtraArgs(template.ExtraArgs); err != nil {
		return err
	}
	capacity := int64(template.NodeCount) * int64(template.Resources.Requests.GPU.Count)
	ranks := int64(template.Parallelism.TP) * int64(template.Parallelism.DP)
	if capacity != ranks {
		return fmt.Errorf("SGLang topology requires %d workers but the Pool provides %d accelerators", ranks, capacity)
	}
	return nil
}

// BuildLaunchPlan projects a verified ModelGroupSpec into the private launch wire contract.
func BuildLaunchPlan(group inferencev1alpha1.ModelGroupSpec) (SglangLaunchPlanV1, error) {
	if err := validateTopology(group.NodeCount, group.MemberCount, group.Role, group.Parallelism); err != nil {
		return SglangLaunchPlanV1{}, err
	}
	if group.Artifacts.Model == "" {
		return SglangLaunchPlanV1{}, fmt.Errorf("SGLang artifacts.model must be nonempty")
	}
	if err := validateExtraArgs(group.Runtime.Args); err != nil {
		return SglangLaunchPlanV1{}, err
	}
	if group.Runtime.InternalGenerateRequestBodyLimitBytes < inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes || group.Runtime.InternalGenerateRequestBodyLimitBytes > inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes {
		return SglangLaunchPlanV1{}, fmt.Errorf("SGLang internal generate request body limit must be between %d and %d", inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes, inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes)
	}
	startup, err := parsePositiveDuration(group.Timeouts.Startup, "startup")
	if err != nil {
		return SglangLaunchPlanV1{}, err
	}
	drain, err := parsePositiveDuration(group.Timeouts.Drain, "drain")
	if err != nil {
		return SglangLaunchPlanV1{}, err
	}
	extra := make([]string, len(group.Runtime.Args))
	for i := range group.Runtime.Args {
		extra[i] = string(group.Runtime.Args[i])
	}
	return SglangLaunchPlanV1{
		Version:                               1,
		Model:                                 group.Artifacts.Model,
		Revision:                              group.Artifacts.ModelRevision,
		TP:                                    group.Parallelism.TP,
		DP:                                    group.Parallelism.DP,
		Port:                                  sglangLoopbackPort,
		StartupSeconds:                        startup,
		DrainSeconds:                          drain,
		ExtraArgs:                             extra,
		InternalGenerateRequestBodyLimitBytes: group.Runtime.InternalGenerateRequestBodyLimitBytes,
	}, nil
}

// JSON returns deterministic output because SglangLaunchPlanV1 uses only ordered
// structs and slices.
func (plan SglangLaunchPlanV1) JSON() (string, error) {
	bytes, err := json.Marshal(plan)
	return string(bytes), err
}

func validateTopology(nodeCount, memberCount int32, role inferencev1alpha1.ModelRole, parallelism inferencev1alpha1.CompiledParallelism) error {
	if nodeCount != 1 || memberCount != 1 {
		return fmt.Errorf("SGLang Groups currently require a single member and node")
	}
	if role != inferencev1alpha1.ModelRoleAggregate {
		return fmt.Errorf("SGLang Groups currently require aggregate role")
	}
	if parallelism.TP < 1 || parallelism.DP < 1 {
		return fmt.Errorf("SGLang topology values must be positive")
	}
	if parallelism.PP != 1 || parallelism.PCP != 1 || parallelism.DCP != 1 || parallelism.EP != nil {
		return fmt.Errorf("SGLang Groups currently require PP=PCP=DCP=1 without expert parallelism")
	}
	return nil
}

func validateExtraArgs(args []inferencev1alpha1.BackendArg) error {
	seen := map[string]bool{}
	for _, raw := range args {
		argument := string(raw)
		if argument == "" || strings.ContainsAny(argument, " \t\r\n") || !strings.HasPrefix(argument, "--") || argument == "--" {
			return fmt.Errorf("SGLang extraArgs must be one nonempty --long-name token")
		}
		name, value, hasValue := strings.Cut(argument, "=")
		if strings.Count(argument, "=") > 1 || strings.Contains(name, "_") || seen[name] {
			return fmt.Errorf("SGLang extraArgs flag %q is not allowed", name)
		}
		seen[name] = true
		if sglangBoolArgs[name] {
			if hasValue {
				return fmt.Errorf("SGLang extraArgs flag %q does not take a value", name)
			}
			continue
		}
		if !sglangValueArgs[name] {
			return fmt.Errorf("SGLang extraArgs flag %q is not allowed", name)
		}
		if !hasValue || value == "" {
			return fmt.Errorf("SGLang extraArgs flag %q requires a value", name)
		}
	}
	return nil
}

func parsePositiveDuration(value inferencev1alpha1.Duration, name string) (int64, error) {
	duration, err := time.ParseDuration(string(value))
	if err != nil || duration <= 0 {
		return 0, fmt.Errorf("SGLang %s timeout must be a positive duration", name)
	}
	seconds := int64(math.Ceil(duration.Seconds()))
	if seconds > maxKubernetesInt32Seconds {
		return 0, fmt.Errorf("SGLang %s timeout must not exceed %d seconds", name, maxKubernetesInt32Seconds)
	}
	return seconds, nil
}
