// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Defines model download bindings and native weight-sharing endpoint compatibility.

package runtimeconfig

import (
	"errors"
	"fmt"
	"net/url"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

const ModelDownloadProxyEnv = "FORETOKEN_MODEL_DOWNLOAD_PROXY"

const (
	ModelExpressGRPCPort     int32 = 6555
	ModelExpressMetadataPort int32 = 5555
)

// ModelDistributionProfile identifies the optional internal model-download service.
type ModelDistributionProfile struct {
	ProxyURL string
}

// ValidateModelExpressPorts checks the loader's native ports for selection and Pod projection.
func ValidateModelExpressPorts(runtimePort, gpuCount int32, pd *api.ModelGroupPDRuntimeConfig) error {
	reservedPorts := []int32{runtimePort, CacheObservationPort(runtimePort)}
	if pd != nil {
		reservedPorts = append(reservedPorts, pd.BootstrapPort)
	}
	for _, base := range []int32{ModelExpressGRPCPort, ModelExpressMetadataPort} {
		if gpuCount < 1 || int64(base)+int64(gpuCount)-1 > 65535 {
			return fmt.Errorf("ModelExpress worker ports exceed the TCP port range")
		}
		for _, reserved := range reservedPorts {
			if reserved >= base && reserved < base+gpuCount {
				return fmt.Errorf("ModelExpress worker ports overlap runtime port %d", reserved)
			}
		}
	}
	if ModelExpressMetadataPort+gpuCount > ModelExpressGRPCPort {
		return fmt.Errorf("ModelExpress metadata and worker ports overlap")
	}
	return nil
}

// Validate checks the internal endpoint before preparation workers receive it.
func (profile ModelDistributionProfile) Validate() error {
	if profile.ProxyURL == "" {
		return nil
	}
	endpoint, err := url.Parse(profile.ProxyURL)
	if err != nil || endpoint.Scheme != "http" || endpoint.Hostname() == "" || endpoint.User != nil ||
		(endpoint.Path != "" && endpoint.Path != "/") || endpoint.RawQuery != "" || endpoint.Fragment != "" {
		return errors.New("model download proxy must be a credential-free internal HTTP endpoint")
	}
	return nil
}
