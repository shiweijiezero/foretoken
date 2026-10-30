// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves service-local speculative decoding dependencies before Pool materialization.
package compiler

import (
	"fmt"
	"path"
	"slices"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
)

// compileSpeculation assigns Draft and Target responsibilities from same-service references.
// The backend adapter alone interprets the native speculative configuration.
func compileSpeculation(spec api.ModelServiceSpec, pools []ModelPool) error {
	byName := make(map[string]int, len(pools))
	for index := range pools {
		byName[pools[index].Name] = index
	}
	referenced := make(map[string]bool)
	for index, entry := range spec.ModelPools {
		if entry.Speculation == nil {
			continue
		}
		if spec.Backend != "vllm" {
			return fmt.Errorf("modelPools %q: speculation requires the vllm backend", entry.Name)
		}
		if entry.Model != "" || pools[index].Template.Role != api.ModelRoleAggregate || len(entry.Speculation.DraftPools) == 0 {
			return fmt.Errorf("modelPools %q: Target speculation requires aggregate execution, draftPools, and the service model", entry.Name)
		}
		for _, name := range entry.Speculation.DraftPools {
			draftIndex, exists := byName[name]
			if !exists {
				return fmt.Errorf("modelPools %q: Draft Pool %q does not exist in this ModelService", entry.Name, name)
			}
			draft := spec.ModelPools[draftIndex]
			if draft.Model == "" || draft.Speculation != nil || pools[draftIndex].Template.Role != api.ModelRoleAggregate {
				return fmt.Errorf("modelPools %q: Draft Pool %q requires proposal model weights and aggregate execution", entry.Name, name)
			}
			referenced[name] = true
		}
		target := &pools[index].Template
		target.SpeculationRole = api.SpeculationRoleTarget
		target.DraftPools = slices.Clone(entry.Speculation.DraftPools)
		slices.Sort(target.DraftPools)
	}
	for index, entry := range spec.ModelPools {
		if entry.Model == "" {
			continue
		}
		if !referenced[entry.Name] {
			return fmt.Errorf("modelPools %q: a proposal model requires a referencing Target speculation.draftPools", entry.Name)
		}
		if spec.Source != api.ModelSourceLocal && path.IsAbs(entry.Model) {
			return fmt.Errorf("modelPools %q: absolute proposal model paths require source local", entry.Name)
		}
		pools[index].Template.Model = entry.Model
		pools[index].Template.SpeculationRole = api.SpeculationRoleDraft
		pools[index].InstanceAdmission = nil
	}
	if len(referenced) == 0 {
		return nil
	}
	resolved := make(map[string]*api.NormalizedPoolTemplate, len(pools))
	for index := range pools {
		resolved[pools[index].Name] = &pools[index].Template
	}
	return vllm.CompileSpeculation(spec.Model, resolved)
}
