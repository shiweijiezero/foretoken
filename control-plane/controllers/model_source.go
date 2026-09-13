// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the platform model-source profile shared by serving workloads.

package controllers

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// ModelSourceProfile configures remote model access for frontend and inference-engine workloads.
type ModelSourceProfile struct {
	Provider        inferencev1alpha1.ModelSourceProvider
	Endpoint        string
	TokenSecretName string
	TokenSecretKey  string
}

// SourceAccess returns the immutable source contract copied into serving workloads.
func (profile ModelSourceProfile) SourceAccess() *inferencev1alpha1.ModelSourceAccess {
	provider := profile.Provider
	if provider == "" {
		provider = inferencev1alpha1.ModelSourceProviderHuggingFace
	}
	return &inferencev1alpha1.ModelSourceAccess{Provider: provider, Endpoint: profile.Endpoint, TokenSecretName: profile.TokenSecretName, TokenSecretKey: profile.TokenSecretKey}
}

// Validate rejects unsupported providers and settings that belong to another source protocol.
func (profile ModelSourceProfile) Validate() error {
	provider := profile.Provider
	if provider == "" {
		provider = inferencev1alpha1.ModelSourceProviderHuggingFace
	}
	if provider != inferencev1alpha1.ModelSourceProviderHuggingFace && provider != inferencev1alpha1.ModelSourceProviderModelScope {
		return fmt.Errorf("model source provider must be huggingface or modelscope")
	}
	if provider == inferencev1alpha1.ModelSourceProviderModelScope && profile.Endpoint != "" {
		return fmt.Errorf("model source endpoint is only supported for the huggingface provider")
	}
	if (profile.TokenSecretName == "") != (profile.TokenSecretKey == "") {
		return fmt.Errorf("model source Secret name and key must be configured together")
	}
	return nil
}
