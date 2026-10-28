// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Compiles native speculative model profiles while keeping Pool discovery controller-owned.
package vllm

import (
	"bytes"
	"encoding/json"
	"fmt"
	"slices"
	"strings"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
)

// CompileSpeculation derives matching native profiles for service-local Draft and Target Pools.
// The caller owns the templates; proposal model selection remains authoritative at the Draft Pool.
func CompileSpeculation(targetModel string, pools map[string]*api.NormalizedPoolTemplate) error {
	names := make([]string, 0, len(pools))
	for name := range pools {
		names = append(names, name)
	}
	slices.Sort(names)
	profiles := make(map[string]apiextensionsv1.JSON)
	for _, name := range names {
		draft := pools[name]
		if draft.SpeculationRole != api.SpeculationRoleDraft {
			continue
		}
		profile, err := nativeDraftProfile(targetModel, draft.Model, draft.EngineArgs)
		if err != nil {
			return fmt.Errorf("modelPools %q: %w", name, err)
		}
		profiles[name] = profile
		draft.EngineArgs = withSpeculativeProfile(draft.EngineArgs, profile)
		// Both roles prepare one shared model/tokenizer/checkpoint publication.
		// The native launcher selects ordinary Draft or neural proposer weights at execution.
		draft.Model = targetModel
	}
	for _, name := range names {
		target := pools[name]
		if target.SpeculationRole != api.SpeculationRoleTarget {
			continue
		}
		profile := profiles[target.DraftPools[0]]
		for _, draft := range target.DraftPools[1:] {
			if !bytes.Equal(profile.Raw, profiles[draft].Raw) {
				return fmt.Errorf("modelPools %q: referenced Draft Pools must use the same native speculative configuration", name)
			}
		}
		for option := range target.EngineArgs {
			if strings.ReplaceAll(option, "_", "-") != "speculative-config" {
				continue
			}
			var config struct {
				Model string `json:"model"`
			}
			if err := json.Unmarshal(profile.Raw, &config); err != nil {
				return err
			}
			draftModel := config.Model
			if draftModel == "" {
				draftModel = targetModel
			}
			provided, err := nativeDraftProfile(targetModel, draftModel, target.EngineArgs)
			if err != nil {
				return fmt.Errorf("modelPools %q: %w", name, err)
			}
			if !bytes.Equal(provided.Raw, profile.Raw) {
				return fmt.Errorf("modelPools %q: Target speculative-config must match its referenced Draft Pools", name)
			}
			break
		}
		target.EngineArgs = withSpeculativeProfile(target.EngineArgs, profile)
	}
	return nil
}

// nativeDraftProfile fills the selected proposal checkpoint into vLLM's native configuration.
// MTP keeps its native same-checkpoint architecture inference when no model was supplied.
func nativeDraftProfile(targetModel, draftModel string, args api.EngineArguments) (apiextensionsv1.JSON, error) {
	config := make(map[string]any)
	found := false
	for name, value := range args {
		if strings.ReplaceAll(name, "_", "-") != "speculative-config" {
			continue
		}
		if found {
			return apiextensionsv1.JSON{}, fmt.Errorf("speculative-config is repeated with different spellings")
		}
		found = true
		if err := json.Unmarshal(value.Raw, &config); err != nil || config == nil {
			return apiextensionsv1.JSON{}, fmt.Errorf("engineArgs.speculative-config must be an object")
		}
	}
	model, _ := config["model"].(string)
	if value, exists := config["model"]; exists && (model == "" || model != draftModel) {
		return apiextensionsv1.JSON{}, fmt.Errorf("speculative-config.model %v must match the Draft Pool model", value)
	}
	if model != "" || config["method"] != "mtp" || draftModel != targetModel {
		config["model"] = draftModel
	}
	raw, err := json.Marshal(config)
	return apiextensionsv1.JSON{Raw: raw}, err
}

// buildDTPlan projects the controller-selected common transport and native proposal inputs.
func buildDTPlan(group api.ModelGroupSpec) (*LaunchDTPlan, error) {
	if group.SpeculationRole == "" {
		return nil, nil
	}
	if group.Role != api.ModelRoleAggregate || (group.SpeculationRole != api.SpeculationRoleDraft && group.SpeculationRole != api.SpeculationRoleTarget) {
		return nil, fmt.Errorf("speculation requires aggregate execution with a Draft or Target responsibility")
	}
	draft, err := DraftArtifacts(runtimeconfig.ProjectModelArtifacts(group.Artifacts), group.Runtime.EngineArgs)
	if err != nil {
		return nil, err
	}
	if group.DTProtocol != "tcp" && group.DTProtocol != "rdma" && group.DTProtocol != "nccl" {
		return nil, fmt.Errorf("speculation requires a resolved DT transport")
	}
	plan := &LaunchDTPlan{Role: group.SpeculationRole, DraftArtifacts: draft, Protocol: group.DTProtocol}
	if group.SpeculationRole == api.SpeculationRoleTarget {
		binding := group.SelectedDraftGroup()
		if binding == nil {
			return nil, fmt.Errorf("Target launch requires a prepared Draft Group binding")
		}
		plan.DraftEndpoint = binding.Endpoint
	}
	return plan, nil
}

func withSpeculativeProfile(args api.EngineArguments, profile apiextensionsv1.JSON) api.EngineArguments {
	args = args.DeepCopy()
	if args == nil {
		args = make(api.EngineArguments)
	}
	delete(args, "speculative_config")
	args["speculative-config"] = *profile.DeepCopy()
	return args
}

// DraftArtifacts selects the additional checkpoint consumed by native speculative startup.
// Preparation and cache collection share the base model's provider and tokenizer lifecycle.
func DraftArtifacts(base runtimeconfig.ModelArtifacts, args api.EngineArguments) (*runtimeconfig.ModelArtifacts, error) {
	var config struct {
		Model    string `json:"model"`
		Revision string `json:"revision"`
	}
	if value, exists := args["speculative-config"]; exists {
		if err := json.Unmarshal(value.Raw, &config); err != nil {
			return nil, fmt.Errorf("decode native Draft checkpoint: %w", err)
		}
	}
	if config.Model == "" || config.Model == base.Model && (config.Revision == "" || config.Revision == base.Revision) {
		return nil, nil
	}
	draft := base
	draft.Model = config.Model
	if config.Revision != "" {
		draft.Revision = config.Revision
	}
	return &draft, nil
}
