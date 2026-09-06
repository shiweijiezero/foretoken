// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines the controller-owned persistent runtime cache profile.

package controllers

import (
	"fmt"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

const runtimeCacheVolumeName = "runtime-cache"

// RuntimeSourceProfile configures optional source access for the runtime adapter.
type RuntimeSourceProfile struct {
	Endpoint        string
	TokenSecretName string
	TokenSecretKey  string
}

// RuntimeSource returns the immutable source contract copied into a ModelPool generation.
func (profile RuntimeSourceProfile) RuntimeSource() *inferencev1alpha1.RuntimeSourceAccess {
	if profile.Endpoint == "" && profile.TokenSecretName == "" {
		return nil
	}
	return &inferencev1alpha1.RuntimeSourceAccess{Endpoint: profile.Endpoint, TokenSecretName: profile.TokenSecretName, TokenSecretKey: profile.TokenSecretKey}
}

// Validate rejects incomplete source credential settings.
func (profile RuntimeSourceProfile) Validate() error {
	if (profile.TokenSecretName == "") != (profile.TokenSecretKey == "") {
		return fmt.Errorf("runtime source Secret name and key must be configured together")
	}
	return nil
}

// RuntimeCacheProfile configures an existing namespace-local cache shared by runtime workloads.
type RuntimeCacheProfile struct {
	ClaimName string
	MountPath string
}

// RuntimeCache returns the immutable cache contract copied into a ModelPool generation.
func (profile RuntimeCacheProfile) RuntimeCache() *inferencev1alpha1.RuntimeCache {
	if profile.ClaimName == "" {
		return nil
	}
	return &inferencev1alpha1.RuntimeCache{ClaimName: profile.ClaimName, MountPath: profile.MountPath}
}

// Validate rejects an incomplete persistent runtime cache configuration.
func (profile RuntimeCacheProfile) Validate() error {
	if profile.ClaimName == "" {
		return nil
	}
	if profile.MountPath == "" || !strings.HasPrefix(profile.MountPath, "/") {
		return fmt.Errorf("cache mount path must be absolute")
	}
	return nil
}
