// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Checks resolved vLLM transfer profiles used by workloads and routing projection.

package vllm

import (
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
)

// CompleteECRuntime checks the encoder-cache transfer fields required by workload and routing consumers.
func CompleteECRuntime(runtime *inferencev1alpha1.ModelGroupECRuntimeConfig, role inferencev1alpha1.ECTransferRole) bool {
	if runtime == nil {
		return false
	}
	return runtime.ProfileName != "" &&
		runtime.ProfileRevision != "" &&
		runtime.Connector == "ECExampleConnector" &&
		runtime.Role == role &&
		runtime.SharedStorageClaim != "" &&
		runtime.SharedStoragePath != ""
}

// CompletePDRuntime checks the Mooncake transfer fields shared by workload and routing validation.
func CompletePDRuntime(runtime *inferencev1alpha1.ModelGroupPDRuntimeConfig) bool {
	return runtime != nil && runtime.ProfileName != "" && runtime.ProfileRevision != "" && runtime.Connector == "MooncakeConnector" && (runtime.Protocol == "rdma" || runtime.Protocol == "tcp") && runtime.BootstrapPort > 0 && runtime.AbortRequestTimeoutSeconds > 0
}
