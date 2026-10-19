// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compiles validated ModelPool templates and ModelGroup contracts for vLLM.

package vllm

import (
	"encoding/json"
	"fmt"
	"math"
	"path"
	"reflect"
	"regexp"
	"slices"
	"sort"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
)

// EffectiveConfig contains the topology and native arguments resolved by the vLLM adapter.
type EffectiveConfig struct {
	Parallelism inferencev1alpha1.CompiledParallelism
	EngineArgs  inferencev1alpha1.EngineArguments
}

// LaunchPlanV1 is the versioned, private Go-to-Rust launch contract. Rust is
// the only component that renders this contract into vLLM command-line flags.
type LaunchPlanV1 struct {
	Version                               int                                `json:"version"`
	NodeCount                             int32                              `json:"nodeCount"`
	Artifacts                             runtimeconfig.ModelArtifacts       `json:"artifacts"`
	Parallelism                           LaunchParallelism                  `json:"parallelism"`
	KV                                    LaunchKVPlan                       `json:"kv"`
	EC                                    *LaunchECPlan                      `json:"ec,omitempty"`
	Lifecycle                             runtimeconfig.LaunchLifecycle      `json:"lifecycle"`
	InternalGenerateRequestBodyLimitBytes int64                              `json:"internalGenerateRequestBodyLimitBytes"`
	EngineArgs                            inferencev1alpha1.EngineArguments  `json:"engineArgs,omitempty"`
	Profiling                             *inferencev1alpha1.ProfilingConfig `json:"profiling,omitempty"`
}

type LaunchParallelism struct {
	TP  int32             `json:"tp"`
	PP  int32             `json:"pp"`
	DP  int32             `json:"dp"`
	PCP int32             `json:"pcp"`
	DCP int32             `json:"dcp"`
	EP  *LaunchExpertPlan `json:"ep,omitempty"`
}

type LaunchExpertPlan struct {
	Backend string `json:"backend,omitempty"`
	EPLB    bool   `json:"eplb"`
}

// LaunchKVPlan uses a closed kind discriminator rather than an untyped vLLM
// config map. KV events are collected independently for every DP rank.
type LaunchKVPlan struct {
	Kind        string `json:"kind"`
	Role        string `json:"role,omitempty"`
	Protocol    string `json:"protocol,omitempty"`
	DeviceName  string `json:"deviceName,omitempty"`
	CPUBytes    int64  `json:"cpuBytes,omitempty"`
	StoragePath string `json:"storagePath,omitempty"`
	Events      bool   `json:"events"`
}

// LaunchECPlan is the sole typed source for --ec-transfer-config. It exposes no
// arbitrary connector arguments, module paths, or peer address fields.
type LaunchECPlan struct {
	ProfileName       string `json:"profileName"`
	ProfileRevision   string `json:"profileRevision"`
	Connector         string `json:"connector"`
	Role              string `json:"role"`
	SharedStoragePath string `json:"sharedStoragePath"`
}

const (
	// FilesystemOffloadMountPath is the writable volume target shared by workload and launch plan.
	FilesystemOffloadMountPath = "/var/lib/foretoken/kv-offload"

	kvNone              = "none"
	kvPD                = "pd"
	kvCPUOffload        = "cpuOffload"
	kvFilesystemOffload = "filesystemOffload"
	kvMooncakeStore     = "mooncakeStore"
	kvMultiConnector    = "multiConnector"
)

// Compile resolves native engine options and topology from the normalized template.
func Compile(template inferencev1alpha1.NormalizedPoolTemplate) (EffectiveConfig, error) {
	var effective EffectiveConfig
	args, err := compileEngineArgs(template)
	if err != nil {
		return EffectiveConfig{}, err
	}
	effective.Parallelism, err = extractParallelism(args)
	if err != nil {
		return EffectiveConfig{}, err
	}
	if len(args) != 0 {
		effective.EngineArgs = args
	}
	if err := validateParallelism(effective.Parallelism); err != nil {
		return EffectiveConfig{}, err
	}
	capacity := int64(template.NodeCount) * int64(template.Resources.Requests.GPU.Count)
	ranks := int64(effective.Parallelism.PP) * int64(effective.Parallelism.TP) * int64(effective.Parallelism.PCP) * int64(effective.Parallelism.DP)
	if capacity != ranks {
		return EffectiveConfig{}, fmt.Errorf("vLLM topology requires %d workers but the Pool provides %d accelerators", ranks, capacity)
	}
	return effective, nil
}

// RequiresRDMA tells the Pool resolver whether the active all-to-all backend needs NICs.
// TP-only EP uses local collectives without activating an all-to-all manager.
func (config EffectiveConfig) RequiresRDMA() bool {
	p := config.Parallelism
	if p.EP == nil || (p.DP == 1 && p.PCP == 1) {
		return false
	}
	// These DeepEP transports initialize GPU-initiated networking even on one node.
	switch p.EP.Backend {
	case "deepep_low_latency", "deepep_v2":
		return true
	default:
		return false
	}
}

// BuildLaunchPlan projects a verified ModelGroupSpec into the private launch wire contract.
func BuildLaunchPlan(group inferencev1alpha1.ModelGroupSpec) (LaunchPlanV1, error) {
	if group.NodeCount < 1 {
		return LaunchPlanV1{}, fmt.Errorf("model-server launch plan requires a positive node count")
	}
	lifecycle, err := runtimeconfig.CompileModelLifecycle(group.Timeouts)
	if err != nil {
		return LaunchPlanV1{}, err
	}
	if group.Artifacts.Model == "" || group.Artifacts.Source == "" || group.Artifacts.ModelRevision == "" || group.Artifacts.Tokenizer == "" || group.Artifacts.TokenizerRevision == "" {
		return LaunchPlanV1{}, fmt.Errorf("vLLM artifacts must be nonempty")
	}
	if err := validateParallelism(group.Parallelism); err != nil {
		return LaunchPlanV1{}, err
	}
	if group.Runtime.InternalGenerateRequestBodyLimitBytes < inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes || group.Runtime.InternalGenerateRequestBodyLimitBytes > inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes {
		return LaunchPlanV1{}, fmt.Errorf("vLLM internal generate request body limit must be between %d and %d", inferencev1alpha1.MinInternalGenerateRequestBodyLimitBytes, inferencev1alpha1.MaxInternalGenerateRequestBodyLimitBytes)
	}
	parallelism := LaunchParallelism{TP: group.Parallelism.TP, PP: group.Parallelism.PP, DP: group.Parallelism.DP, PCP: group.Parallelism.PCP, DCP: group.Parallelism.DCP}
	if group.Parallelism.EP != nil {
		parallelism.EP = &LaunchExpertPlan{Backend: group.Parallelism.EP.Backend, EPLB: group.Parallelism.EP.EPLB}
	}
	kv, err := buildKVPlan(group)
	if err != nil {
		return LaunchPlanV1{}, err
	}
	ec, err := buildECPlan(group)
	if err != nil {
		return LaunchPlanV1{}, err
	}
	return LaunchPlanV1{Version: 1, NodeCount: group.NodeCount, Artifacts: runtimeconfig.ProjectModelArtifacts(group.Artifacts), Parallelism: parallelism, KV: kv, EC: ec, Lifecycle: lifecycle, InternalGenerateRequestBodyLimitBytes: group.Runtime.InternalGenerateRequestBodyLimitBytes, EngineArgs: group.Runtime.EngineArgs.DeepCopy(), Profiling: group.Runtime.Profiling.DeepCopy()}, nil
}

// JSON returns deterministic output because LaunchPlanV1 uses only ordered structs and slices.
func (plan LaunchPlanV1) JSON() (string, error) {
	bytes, err := json.Marshal(plan)
	return string(bytes), err
}

// buildKVPlan projects the ModelGroup cache runtime into the closed vLLM KV launch plan.
func buildKVPlan(group inferencev1alpha1.ModelGroupSpec) (LaunchKVPlan, error) {
	role := "kv_both"
	if group.Role == inferencev1alpha1.ModelRolePrefill {
		role = "kv_producer"
	} else if group.Role == inferencev1alpha1.ModelRoleDecode {
		role = "kv_consumer"
	}
	plan := LaunchKVPlan{Kind: kvNone, Events: true}
	if group.PDRuntime != nil && group.KVRuntime == nil {
		return LaunchKVPlan{Kind: kvPD, Role: role, Protocol: group.PDRuntime.Protocol, DeviceName: group.PDRuntime.RDMADeviceName, Events: plan.Events}, nil
	}
	if group.KVRuntime == nil {
		return plan, nil
	}
	if group.KVRuntime.Offload != nil {
		offload := group.KVRuntime.Offload
		if offload.CPUBytes < 1 {
			return LaunchKVPlan{}, fmt.Errorf("vLLM KV offload cpuBytes must be positive")
		}
		kind := kvCPUOffload
		storagePath := ""
		if offload.Filesystem {
			kind = kvFilesystemOffload
			storagePath = FilesystemOffloadMountPath
		}
		return LaunchKVPlan{Kind: kind, CPUBytes: offload.CPUBytes, StoragePath: storagePath, Events: plan.Events}, nil
	}
	if group.KVRuntime.MooncakeStore != nil {
		if group.PDRuntime != nil {
			return LaunchKVPlan{Kind: kvMultiConnector, Role: role, Protocol: group.PDRuntime.Protocol, DeviceName: group.PDRuntime.RDMADeviceName, Events: plan.Events}, nil
		}
		storeRole := "kv_both"
		if group.Role == inferencev1alpha1.ModelRoleDecode {
			storeRole = "kv_consumer"
		}
		return LaunchKVPlan{Kind: kvMooncakeStore, Role: storeRole, Events: plan.Events}, nil
	}
	return LaunchKVPlan{}, fmt.Errorf("vLLM KV runtime must select a backend")
}

// buildECPlan validates and projects EC runtime settings into the vLLM launch plan.
func buildECPlan(group inferencev1alpha1.ModelGroupSpec) (*LaunchECPlan, error) {
	if group.ECRuntime == nil {
		return nil, nil
	}
	ec := group.ECRuntime
	if ec.ProfileName == "" || ec.ProfileRevision == "" || ec.Connector != "ECExampleConnector" || ec.SharedStorageClaim == "" || ec.SharedStoragePath == "" {
		return nil, fmt.Errorf("vLLM EC runtime config is incomplete")
	}
	if group.Role == inferencev1alpha1.ModelRoleEncoder && ec.Role != inferencev1alpha1.ECTransferRoleProducer {
		return nil, fmt.Errorf("vLLM encoder requires EC producer runtime")
	}
	if group.Role == inferencev1alpha1.ModelRolePrefill && ec.Role != inferencev1alpha1.ECTransferRoleConsumer {
		return nil, fmt.Errorf("vLLM prefill requires EC consumer runtime")
	}
	if group.Role != inferencev1alpha1.ModelRoleEncoder && group.Role != inferencev1alpha1.ModelRolePrefill {
		return nil, fmt.Errorf("vLLM EC runtime is only valid for encoder and prefill")
	}
	return &LaunchECPlan{
		ProfileName: ec.ProfileName, ProfileRevision: ec.ProfileRevision,
		Connector: ec.Connector, Role: string(ec.Role),
		SharedStoragePath: path.Join(ec.SharedStoragePath, runtimeconfig.EncoderCacheDirectory(ec)),
	}, nil
}

// extractParallelism consumes native topology options into the runtime's worker layout.
// Size defaults match vLLM; EP uses the TP × PCP × DP ranks rather than a separate user size.
func extractParallelism(args inferencev1alpha1.EngineArguments) (inferencev1alpha1.CompiledParallelism, error) {
	p := inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1}
	for name, target := range map[string]*int32{
		"tensor-parallel-size": &p.TP, "pipeline-parallel-size": &p.PP,
		"data-parallel-size": &p.DP, "prefill-context-parallel-size": &p.PCP,
		"decode-context-parallel-size": &p.DCP,
	} {
		if value, ok := args[name]; ok {
			if string(value.Raw) != "null" {
				if err := json.Unmarshal(value.Raw, target); err != nil || *target < 1 {
					return p, fmt.Errorf("engineArgs.%s must be a positive integer", name)
				}
			}
			delete(args, name)
		}
	}
	var ep, eplb bool
	for name, target := range map[string]*bool{"enable-expert-parallel": &ep, "enable-eplb": &eplb} {
		if value, ok := args[name]; ok {
			if err := json.Unmarshal(value.Raw, target); err != nil {
				return p, fmt.Errorf("engineArgs.%s must be a boolean", name)
			}
			delete(args, name)
		}
	}
	if eplb && !ep {
		return p, fmt.Errorf("enable-eplb requires enable-expert-parallel")
	}
	if ep {
		size := int64(p.TP) * int64(p.PCP) * int64(p.DP)
		if size > math.MaxInt32 {
			return p, fmt.Errorf("expert-parallel rank count exceeds supported resource capacity")
		}
		p.EP = &inferencev1alpha1.ExpertParallelism{Size: int32(size), EPLB: eplb}
		if value, ok := args["all2all-backend"]; ok {
			if err := json.Unmarshal(value.Raw, &p.EP.Backend); err != nil {
				return p, fmt.Errorf("engineArgs.all2all-backend must be a string")
			}
			delete(args, "all2all-backend")
		}
	}
	return p, nil
}

// CompatibleKVTransfer reports whether controller-selected Groups can exchange Mooncake KV.
// Worker and scheduler sizing may differ; model interpretation and cache representation may not.
func CompatibleKVTransfer(left, right inferencev1alpha1.ModelGroupSpec) bool {
	if left.Runtime.Backend != right.Runtime.Backend || left.Runtime.Image != right.Runtime.Image {
		return false
	}
	leftTP, rightTP := left.Parallelism.TP, right.Parallelism.TP
	if leftTP < 1 || rightTP < 1 || (leftTP%rightTP != 0 && rightTP%leftTP != 0) {
		return false
	}
	names := make(map[string]struct{}, len(left.Runtime.EngineArgs)+len(right.Runtime.EngineArgs))
	for name := range left.Runtime.EngineArgs {
		names[name] = struct{}{}
	}
	for name := range right.Runtime.EngineArgs {
		names[name] = struct{}{}
	}
	for name := range names {
		switch name {
		case "gpu-memory-utilization", "kv-cache-memory-bytes", "max-num-seqs", "max-num-batched-tokens", "max-model-len", "enforce-eager", "compilation-config", "cuda-graph-sizes", "max-cudagraph-capture-size", "scheduling-policy", "enable-chunked-prefill", "mm-processor-cache-gb", "mm-encoder-only", "distributed-executor-backend", "data-parallel-backend":
			continue
		}
		var leftValue, rightValue any
		if value, ok := left.Runtime.EngineArgs[name]; ok {
			if json.Unmarshal(value.Raw, &leftValue) != nil {
				return false
			}
		}
		if value, ok := right.Runtime.EngineArgs[name]; ok {
			if json.Unmarshal(value.Raw, &rightValue) != nil {
				return false
			}
		}
		if !reflect.DeepEqual(leftValue, rightValue) {
			return false
		}
	}
	return true
}

func validateParallelism(parallelism inferencev1alpha1.CompiledParallelism) error {
	if parallelism.TP < 1 || parallelism.PP < 1 || parallelism.DP < 1 || parallelism.PCP < 1 || parallelism.DCP < 1 {
		return fmt.Errorf("vLLM topology values must be positive")
	}
	if parallelism.EP != nil && parallelism.EP.EPLB && parallelism.EP.Size == 1 {
		return fmt.Errorf("vLLM EPLB requires more than one expert-parallel rank")
	}
	return nil
}

var engineArgName = regexp.MustCompile(`^[a-z][a-z0-9_-]*$`)

// compileEngineArgs resolves native choices against the Pool's actual execution contract.
// Unclaimed options remain engine-owned; the Rust adapter renders the resulting map.
func compileEngineArgs(template inferencev1alpha1.NormalizedPoolTemplate) (inferencev1alpha1.EngineArguments, error) {
	input := template.EngineArgs
	reserved := map[string]string{
		"model":                                "select the model with spec.model",
		"tokenizer":                            "select the tokenizer with spec.tokenizer",
		"revision":                             "model preparation resolves the model revision before engine startup",
		"code-revision":                        "model code is loaded from the prepared model snapshot",
		"tokenizer-revision":                   "model preparation resolves the tokenizer revision before engine startup",
		"served-model-name":                    "the service model identity is shared by routing and engine metadata",
		"config":                               "place native options directly in engineArgs so topology and resource planning use them",
		"hf-token":                             "configure Hugging Face credentials for model preparation, not only the engine process",
		"download-dir":                         "model preparation and runtimeCache own the model download directory",
		"headless":                             "the model-server connects directly to EngineCore without a Python API server",
		"api-server-count":                     "the model-server owns API serving",
		"grpc":                                 "the model-server uses the EngineCore transport, not the Python gRPC server",
		"host":                                 "configure the Foretoken service endpoint rather than the unused Python API server",
		"port":                                 "configure the Foretoken service endpoint rather than the unused Python API server",
		"data-parallel-address":                "the model-server owns the EngineCore handshake endpoint",
		"data-parallel-rpc-port":               "the model-server owns the EngineCore handshake endpoint",
		"data-parallel-rank":                   "ranks are assigned from the Pool topology",
		"data-parallel-start-rank":             "ranks are assigned from the Pool topology",
		"data-parallel-size-local":             "local engine counts are derived from the Pool topology",
		"data-parallel-external-lb":            "Foretoken routes requests to its managed DP ranks",
		"data-parallel-hybrid-lb":              "Foretoken routes requests to its managed DP ranks",
		"data-parallel-multi-port-external-lb": "Foretoken routes requests to its managed DP ranks",
		"enable-elastic-ep":                    "the Pool owns worker membership and replica lifecycle",
		"master-addr":                          "member discovery supplies the distributed rendezvous address",
		"master-port":                          "member discovery supplies the distributed rendezvous port",
		"nnodes":                               "select the node count with nodes",
		"node-rank":                            "member discovery assigns each node rank",
		"kv-events-config":                     "the model-server owns the publisher and subscriber configuration",
		"kv-transfer-config":                   "select KV transfer through kvCache or split modelPools so routing and storage share the same configuration",
		"ec-transfer-config":                   "select encoder transfer through ecProfile and E/P/D modelPools",
	}
	if template.RuntimeCache != nil {
		reserved["profiler-config"] = "Foretoken profiling owns capture configuration and output storage"
	}
	if template.Profiling != nil && template.Profiling.Engine == "mctracer" {
		reserved["worker-cls"] = "mcTracer profiling supplies the instrumented worker"
	}
	args := make(inferencev1alpha1.EngineArguments, len(input))
	names := make([]string, 0, len(input))
	for name := range input {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		key := strings.ReplaceAll(name, "_", "-")
		if !engineArgName.MatchString(name) || strings.HasPrefix(key, "no-") {
			return nil, fmt.Errorf("engineArgs key %q must be a full option name without --; use YAML booleans for switches", name)
		}
		for _, topology := range []string{"tensor-parallel-size", "pipeline-parallel-size", "data-parallel-size", "prefill-context-parallel-size", "decode-context-parallel-size", "enable-expert-parallel", "enable-eplb", "all2all-backend"} {
			if key != topology && strings.HasPrefix(topology, key) {
				return nil, fmt.Errorf("engineArgs.%s must use the full option name %s", name, topology)
			}
		}
		if string(input[name].Raw) == "null" {
			continue
		}
		if reason, exists := reserved[key]; exists {
			return nil, fmt.Errorf("engineArgs.%s cannot be set here: %s", name, reason)
		}
		for owned := range reserved {
			if key != "data-parallel-size" && strings.HasPrefix(owned, key) {
				return nil, fmt.Errorf("engineArgs.%s must use a full native option name", name)
			}
		}
		for _, option := range []string{"runner", "convert", "data-parallel-backend", "distributed-executor-backend", "mm-device-do-normalize", "mm-encoder-only", "enforce-eager", "enable-prefix-caching"} {
			if key != option && strings.HasPrefix(option, key) {
				return nil, fmt.Errorf("engineArgs.%s must use the full option name %s", name, option)
			}
		}
		if _, exists := args[key]; exists {
			return nil, fmt.Errorf("engineArgs repeats option %q with different spellings", key)
		}
		value := input[name]
		args[key] = *value.DeepCopy()
	}

	// The request transport serves generation and launches DP engines as local processes.
	for _, choice := range []struct {
		name   string
		values []string
		reason string
	}{
		{"runner", []string{"auto", "generate"}, "the model-server submits generation requests"},
		{"convert", []string{"auto", "none"}, "the model-server does not submit pooling requests"},
		{"data-parallel-backend", []string{"mp"}, "headless EngineCore startup launches DP processes"},
	} {
		if value, exists := args[choice.name]; exists {
			var selected string
			if json.Unmarshal(value.Raw, &selected) != nil || !slices.Contains(choice.values, selected) {
				return nil, fmt.Errorf("engineArgs.%s requires %v: %s", choice.name, choice.values, choice.reason)
			}
		}
	}

	// Compile role requirements into the effective configuration, rather than overriding argv.
	required := map[string]any{}
	if template.Role == inferencev1alpha1.ModelRoleEncoder {
		required["mm-encoder-only"] = true
		required["enable-prefix-caching"] = false
		// Preserve the Encoder default while allowing native graph execution choices.
		if _, supplied := args["enforce-eager"]; !supplied {
			required["enforce-eager"] = true
		}
	} else if _, exists := args["mm-encoder-only"]; exists {
		required["mm-encoder-only"] = false
	}
	if template.NodeCount > 1 {
		required["distributed-executor-backend"] = "mp"
		required["data-parallel-backend"] = "mp"
	}
	// Pixel normalization is already performed by the Rust image processor. The launcher
	// emits the disabling flag only for engine versions that expose this option.
	if value, exists := args["mm-device-do-normalize"]; exists {
		var enabled bool
		if json.Unmarshal(value.Raw, &enabled) != nil || enabled {
			return nil, fmt.Errorf("engineArgs.mm-device-do-normalize must be false because the image processor already normalizes pixels")
		}
		delete(args, "mm-device-do-normalize")
	}
	for _, name := range []string{"mm-encoder-only", "enforce-eager", "enable-prefix-caching", "distributed-executor-backend", "data-parallel-backend"} {
		expected, applies := required[name]
		if !applies {
			continue
		}
		value, exists := args[name]
		if exists {
			var selected any
			if json.Unmarshal(value.Raw, &selected) != nil || !reflect.DeepEqual(selected, expected) {
				return nil, fmt.Errorf("engineArgs.%s must be %v for this Pool's execution mode", name, expected)
			}
		}
		value.Raw, _ = json.Marshal(expected)
		args[name] = value
	}
	if len(args) == 0 {
		return nil, nil
	}
	return args, nil
}
