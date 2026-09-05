// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Validates the control-plane runtime profile before manager startup.
package runtimeconfig

import "errors"

const (
	ecConnector = "ECExampleConnector"
	pdProtocol  = "rdma"
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

// PDProfile contains the platform-owned Mooncake P/D settings.
type PDProfile struct {
	Name                       string
	Revision                   string
	Protocol                   string
	BootstrapPort              int
	AbortRequestTimeoutSeconds int
	RDMADeviceName             string
	RDMAResourceName           string
	RDMAResourceCount          int
}

// MooncakeStoreProfile contains the platform-owned external Store settings.
type MooncakeStoreProfile struct {
	Name           string
	Revision       string
	ConfigMapName  string
	ConfigMapKey   string
	PythonHashSeed string
}

// Profiles contains the startup values for optional vLLM runtime profiles.
type Profiles struct {
	EC            ECProfile
	PD            PDProfile
	MooncakeStore MooncakeStoreProfile
}

// Validate rejects incomplete enabled profiles and stray disabled settings.
func (profiles Profiles) Validate() error {
	if err := validateEC(profiles.EC); err != nil {
		return err
	}
	if err := validatePD(profiles.PD); err != nil {
		return err
	}
	return validateMooncakeStore(profiles.MooncakeStore)
}

func validateEC(profile ECProfile) error {
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

func validatePD(profile PDProfile) error {
	if profile.Name == "" {
		if profile.Revision != "" || profile.Protocol != "" || profile.BootstrapPort != 0 || profile.AbortRequestTimeoutSeconds != 0 || profile.RDMADeviceName != "" || profile.RDMAResourceName != "" || profile.RDMAResourceCount != 0 {
			return errors.New("vLLM P/D settings require vllm-pd-profile-name")
		}
		return nil
	}
	if profile.Revision == "" || profile.Protocol != pdProtocol || profile.BootstrapPort < 1 || profile.BootstrapPort > 65535 || profile.AbortRequestTimeoutSeconds < 1 || int64(profile.AbortRequestTimeoutSeconds) > maxInt32 || profile.RDMADeviceName == "" || profile.RDMAResourceName == "" || profile.RDMAResourceCount < 1 || int64(profile.RDMAResourceCount) > maxInt32 {
		return errors.New("vLLM P/D profile is incomplete or unsupported")
	}
	return nil
}

func validateMooncakeStore(profile MooncakeStoreProfile) error {
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
