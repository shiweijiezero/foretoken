// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects model sources while retaining the selected application's preparation contract.
package runtimeconfig

import api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"

// ModelArtifactsEnv carries the engine-neutral input consumed by preparation Jobs.
const ModelArtifactsEnv = "FORETOKEN_MODEL_ARTIFACTS"

// SharedModelCachePreparationVersion selects engine-neutral preparation with shared-cache references.
const SharedModelCachePreparationVersion int32 = 2

// ModelArtifacts identifies model and tokenizer sources for preparation and engine launch.
type ModelArtifacts struct {
	Model             string          `json:"model"`
	Source            api.ModelSource `json:"source"`
	Revision          string          `json:"revision"`
	Tokenizer         string          `json:"tokenizer"`
	TokenizerRevision string          `json:"tokenizerRevision"`
}

// ModelPreparationVersion retains an application's acquisition contract across controller upgrades.
// Selections written before this field used vLLM preparation v1 and native Omni acquisition.
func ModelPreparationVersion(selectedVersion *int32, backend string) int32 {
	if selectedVersion != nil {
		return *selectedVersion
	}
	if backend == "vllm" {
		return 1
	}
	return 0
}

// ResolveModelArtifacts projects retained Pool source intent with tokenizer defaults.
// It does not validate engine configuration, so paused Pools can retain their source independently.
func ResolveModelArtifacts(template api.NormalizedPoolTemplate) ModelArtifacts {
	artifacts := ModelArtifacts{
		Model: template.Model, Source: template.Source, Revision: template.ModelRevision,
		Tokenizer: template.Tokenizer, TokenizerRevision: template.TokenizerRevision,
	}
	if artifacts.Tokenizer == "" {
		artifacts.Tokenizer = artifacts.Model
	}
	if artifacts.TokenizerRevision == "" {
		artifacts.TokenizerRevision = artifacts.Revision
	}
	return artifacts
}

// ProjectModelArtifacts selects immutable Group inputs for the preparation and launch protocols.
func ProjectModelArtifacts(artifacts api.ModelGroupArtifacts) ModelArtifacts {
	return ModelArtifacts{
		Model: artifacts.Model, Source: artifacts.Source, Revision: artifacts.ModelRevision,
		Tokenizer: artifacts.Tokenizer, TokenizerRevision: artifacts.TokenizerRevision,
	}
}
