// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Validates the control-plane runtime profile before manager startup.
package runtimeconfig

import "errors"

const (
	ecConnector = "ECExampleConnector"
	maxInt32    = int64(1<<31 - 1)
)

// ECProfile contains the platform-owned encoder/prefill connector settings.
type ECProfile struct {
	Name               string
	Revision           string
	Connector          string
	SharedStorageClaim string
	SharedStoragePath  string
}

// MooncakeStoreProfile contains the platform-owned external Store settings.
type MooncakeStoreProfile struct {
	Name           string
	Revision       string
	ConfigMapName  string
	ConfigMapKey   string
	PythonHashSeed string
}

// RDMAProfile selects the device allocation shared by engine collectives and connectors.
type RDMAProfile struct {
	ResourceName  string
	ResourceCount int
}

// Profiles contains the startup values for optional vLLM runtime profiles.
type Profiles struct {
	EC            ECProfile
	RDMA          RDMAProfile
	MooncakeStore MooncakeStoreProfile
}

// Validate rejects incomplete enabled profiles and stray disabled settings.
func (profiles Profiles) Validate() error {
	if profiles.RDMA.ResourceName != "" && (profiles.RDMA.ResourceCount < 1 || int64(profiles.RDMA.ResourceCount) > maxInt32) {
		return errors.New("RDMA resource count must be a positive int32")
	}
	if err := profiles.EC.Validate(); err != nil {
		return err
	}
	return profiles.MooncakeStore.Validate()
}

// Validate checks the encoder profile consumed by startup and Group resolution.
func (profile ECProfile) Validate() error {
	if profile.Name == "" {
		if profile.Revision != "" || profile.SharedStorageClaim != "" {
			return errors.New("vLLM EC settings require vllm-ec-profile-name")
		}
		return nil
	}
	if profile.Revision == "" || profile.Connector != ecConnector || profile.SharedStorageClaim == "" || profile.SharedStoragePath == "" {
		return errors.New("vLLM EC profile is incomplete")
	}
	return nil
}

// Validate checks the external Store binding shared by startup and Group resolution.
func (profile MooncakeStoreProfile) Validate() error {
	if profile.Name == "" {
		if profile.Revision != "" || profile.ConfigMapName != "" || profile.ConfigMapKey != "" || profile.PythonHashSeed != "" {
			return errors.New("Mooncake Store settings require vllm-mooncake-store-profile-name")
		}
		return nil
	}
	if profile.Revision == "" || profile.ConfigMapName == "" || profile.ConfigMapKey == "" || profile.PythonHashSeed == "" {
		return errors.New("Mooncake Store profile is incomplete")
	}
	return nil
}
