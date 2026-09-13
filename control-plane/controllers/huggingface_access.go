// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines platform access settings for Hugging Face model repositories.

package controllers

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// HuggingFaceAccessProfile configures an optional endpoint and credential for Hugging Face models.
type HuggingFaceAccessProfile struct {
	Endpoint        string
	TokenSecretName string
	TokenSecretKey  string
}

// Access returns the immutable settings copied only into Hugging Face workloads.
func (profile HuggingFaceAccessProfile) Access() *inferencev1alpha1.HuggingFaceAccess {
	if profile.Endpoint == "" && profile.TokenSecretName == "" {
		return nil
	}
	return &inferencev1alpha1.HuggingFaceAccess{Endpoint: profile.Endpoint, TokenSecretName: profile.TokenSecretName, TokenSecretKey: profile.TokenSecretKey}
}

// Validate rejects an incomplete credential reference.
func (profile HuggingFaceAccessProfile) Validate() error {
	if (profile.TokenSecretName == "") != (profile.TokenSecretKey == "") {
		return fmt.Errorf("Hugging Face Secret name and key must be configured together")
	}
	return nil
}
