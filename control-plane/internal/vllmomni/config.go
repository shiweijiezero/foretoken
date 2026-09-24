// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compiles the intentionally small vLLM-Omni runtime contract.

package vllmomni

import (
	"encoding/json"
	"fmt"
	"regexp"
	"strings"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
)

const Backend = "vllm-omni"

type EffectiveConfig struct {
	Model             string
	Source            inferencev1alpha1.ModelSource
	Revision          string
	Tokenizer         string
	TokenizerRevision string
	Parallelism       inferencev1alpha1.CompiledParallelism
	EngineArgs        inferencev1alpha1.EngineArguments
}

type LaunchPlanV1 struct {
	Version      int                               `json:"version"`
	Model        string                            `json:"model"`
	Source       inferencev1alpha1.ModelSource     `json:"source"`
	Revision     string                            `json:"revision"`
	UpstreamPort int32                             `json:"upstreamPort"`
	Lifecycle    LaunchLifecycle                   `json:"lifecycle"`
	EngineArgs   inferencev1alpha1.EngineArguments `json:"engineArgs,omitempty"`
}

type LaunchLifecycle struct {
	StartupSeconds int64 `json:"startupSeconds"`
	DrainSeconds   int64 `json:"drainSeconds"`
}

func Compile(template inferencev1alpha1.NormalizedPoolTemplate) (EffectiveConfig, error) {
	if template.NodeCount != 1 || template.MemberCount != 1 {
		return EffectiveConfig{}, fmt.Errorf("vLLM-Omni initially supports one member on one node")
	}
	if template.Role != inferencev1alpha1.ModelRoleAggregate {
		return EffectiveConfig{}, fmt.Errorf("vLLM-Omni initially supports only aggregate Pools")
	}
	if template.KVCache != nil || template.ECProfile != "" || template.Profiling != nil {
		return EffectiveConfig{}, fmt.Errorf("vLLM-Omni does not yet support KV, E/P/D, or Foretoken profiling configuration")
	}
	gpus := template.Resources.Requests.GPU.Count
	if gpus < 1 {
		return EffectiveConfig{}, fmt.Errorf("vLLM-Omni requires at least one accelerator")
	}
	args, err := compileEngineArgs(template.EngineArgs)
	if err != nil {
		return EffectiveConfig{}, err
	}
	for name, expected := range map[string]int32{
		"num-gpus": gpus, "tensor-parallel-size": gpus,
		"usp": 1, "ring": 1,
	} {
		if err := setOrValidateIntArg(args, name, expected); err != nil {
			return EffectiveConfig{}, err
		}
	}
	for _, name := range []string{"pipeline-parallel-size", "data-parallel-size"} {
		if value, present, err := intArg(args, name); err != nil {
			return EffectiveConfig{}, err
		} else if present && value != 1 {
			return EffectiveConfig{}, fmt.Errorf("engineArgs.%s must be 1 for the initial vLLM-Omni runtime", name)
		}
	}
	tokenizer := template.Tokenizer
	if tokenizer == "" {
		tokenizer = template.Model
	}
	tokenizerRevision := template.TokenizerRevision
	if tokenizerRevision == "" {
		tokenizerRevision = template.ModelRevision
	}
	return EffectiveConfig{
		Model: template.Model, Source: template.Source, Revision: template.ModelRevision,
		Tokenizer: tokenizer, TokenizerRevision: tokenizerRevision,
		Parallelism: inferencev1alpha1.CompiledParallelism{TP: gpus, PP: 1, DP: 1, PCP: 1, DCP: 1},
		EngineArgs:  args,
	}, nil
}

func BuildLaunchPlan(group inferencev1alpha1.ModelGroupSpec) (LaunchPlanV1, error) {
	if group.Runtime.Backend != Backend {
		return LaunchPlanV1{}, fmt.Errorf("vLLM-Omni launch plan requires backend %q", Backend)
	}
	if group.Artifacts.Model == "" || group.Artifacts.Source == "" || group.Artifacts.ModelRevision == "" {
		return LaunchPlanV1{}, fmt.Errorf("vLLM-Omni model, source, and revision must be nonempty")
	}
	if group.Runtime.Port < 1 || group.Runtime.Port > 65533 {
		return LaunchPlanV1{}, fmt.Errorf("vLLM-Omni model-server port must be between 1 and 65533")
	}
	startup, err := runtimeconfig.PositiveDurationSeconds(group.Timeouts.Startup, "vLLM-Omni startup")
	if err != nil {
		return LaunchPlanV1{}, err
	}
	drain, err := runtimeconfig.PositiveDurationSeconds(group.Timeouts.Drain, "vLLM-Omni drain")
	if err != nil {
		return LaunchPlanV1{}, err
	}
	return LaunchPlanV1{
		Version: 1, Model: group.Artifacts.Model, Source: group.Artifacts.Source,
		Revision: group.Artifacts.ModelRevision, UpstreamPort: group.Runtime.Port + 2,
		Lifecycle:  LaunchLifecycle{StartupSeconds: startup, DrainSeconds: drain},
		EngineArgs: group.Runtime.EngineArgs.DeepCopy(),
	}, nil
}

func (plan LaunchPlanV1) JSON() (string, error) {
	data, err := json.Marshal(plan)
	return string(data), err
}

var engineArgName = regexp.MustCompile(`^[a-z][a-z0-9_-]*$`)

func compileEngineArgs(input inferencev1alpha1.EngineArguments) (inferencev1alpha1.EngineArguments, error) {
	args := make(inferencev1alpha1.EngineArguments, len(input)+4)
	for name, value := range input {
		key := strings.ReplaceAll(name, "_", "-")
		if !engineArgName.MatchString(name) || strings.HasPrefix(key, "no-") {
			return nil, fmt.Errorf("engineArgs key %q must be a full option name without --", name)
		}
		if key == "model" || key == "omni" || key == "host" || key == "port" || key == "revision" {
			return nil, fmt.Errorf("engineArgs option %q is owned by Foretoken", name)
		}
		if _, exists := args[key]; exists {
			return nil, fmt.Errorf("engineArgs repeats option %q with different spellings", key)
		}
		args[key] = *value.DeepCopy()
	}
	return args, nil
}

func setOrValidateIntArg(args inferencev1alpha1.EngineArguments, name string, expected int32) error {
	value, present, err := intArg(args, name)
	if err != nil {
		return err
	}
	if present {
		if value != expected {
			return fmt.Errorf("engineArgs.%s must be %d for the selected accelerator topology", name, expected)
		}
		return nil
	}
	args[name] = apiextensionsv1.JSON{Raw: []byte(fmt.Sprintf("%d", expected))}
	return nil
}

func intArg(args inferencev1alpha1.EngineArguments, name string) (int32, bool, error) {
	value, present := args[name]
	if !present {
		return 0, false, nil
	}
	var parsed int32
	if err := json.Unmarshal(value.Raw, &parsed); err != nil || parsed < 1 {
		return 0, true, fmt.Errorf("engineArgs.%s must be a positive integer", name)
	}
	return parsed, true, nil
}
