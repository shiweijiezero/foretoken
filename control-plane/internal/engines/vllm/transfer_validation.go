// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves and checks vLLM transfer settings shared by workloads and routing projection.

package vllm

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
)

// ResolvePDRuntime gives the Pool resolver native transport settings and an externally published
// listener range. Private engine ports are assigned around this range by the model-server.
func ResolvePDRuntime(template inferencev1alpha1.NormalizedPoolTemplate, parallelism inferencev1alpha1.CompiledParallelism, runtimePort int32) (*inferencev1alpha1.ModelGroupPDRuntimeConfig, error) {
	if template.Role != inferencev1alpha1.ModelRolePrefill && template.Role != inferencev1alpha1.ModelRoleDecode {
		return nil, nil
	}
	if template.KVTransfer == nil {
		return nil, fmt.Errorf("P/D Pool requires compiled kvTransfer settings")
	}
	intent := template.KVTransfer
	if intent.Protocol != "tcp" && intent.Protocol != "rdma" {
		return nil, fmt.Errorf("P/D Pool requires a resolved TCP or RDMA transport")
	}
	var connector string
	var preferred, count int64
	switch intent.Connector {
	case "mooncake":
		connector, preferred, count = inferencev1alpha1.MooncakeConnector, 8998, 1
	case "nixl":
		connector, preferred, count = inferencev1alpha1.NixlConnector, 5600, int64(parallelism.DP)
	default:
		return nil, fmt.Errorf("unsupported P/D connector %q", intent.Connector)
	}
	// Each free interval starts at port 1 or just after an owned listener.
	// Prefer the native port, then choose an interval large enough for all DP ranks.
	reserved := []int64{int64(runtimePort)}
	if template.RuntimeCache != nil {
		reserved = append(reserved, int64(runtimeconfig.CacheObservationPort(runtimePort)))
	}
	candidates := []int64{preferred}
	for _, port := range reserved {
		candidates = append(candidates, port+1)
	}
	candidates = append(candidates, 1)
	for _, base := range candidates {
		end := base + count
		if count < 1 || end > 65536 {
			continue
		}
		conflict := false
		for _, port := range reserved {
			if port >= base && port < end {
				conflict = true
				break
			}
		}
		if !conflict {
			return &inferencev1alpha1.ModelGroupPDRuntimeConfig{
				Connector: connector, Protocol: intent.Protocol, BootstrapPort: int32(base),
			}, nil
		}
	}
	return nil, fmt.Errorf("no port range is available for the P/D connector listeners")
}

// CompleteECRuntime checks the encoder-cache transfer fields required by workload and routing consumers.
func CompleteECRuntime(runtime *inferencev1alpha1.ModelGroupECRuntimeConfig, role inferencev1alpha1.ECTransferRole) bool {
	if runtime == nil {
		return false
	}
	return runtime.ProfileName != "" &&
		runtime.ProfileRevision != "" &&
		runtime.Connector == ecConnector &&
		runtime.Role == role &&
		runtime.SharedStorageClaim != "" &&
		runtime.SharedStoragePath != ""
}

// CompletePDRuntime checks the connector fields shared by workload and routing validation.
func CompletePDRuntime(runtime *inferencev1alpha1.ModelGroupPDRuntimeConfig) bool {
	return runtime != nil &&
		(runtime.Connector == inferencev1alpha1.MooncakeConnector || runtime.Connector == inferencev1alpha1.NixlConnector) &&
		(runtime.Protocol == "rdma" || runtime.Protocol == "tcp") &&
		runtime.BootstrapPort > 0 &&
		runtime.BootstrapPort <= 65535 &&
		runtime.AbortRequestTimeoutSeconds >= 0
}
