// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Validates resolved model execution contracts before workload creation.

package model

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
)

// validateGroupProfile checks the resolved execution contract before Group resources are created.
func validateGroupProfile(group *inferencev1alpha1.ModelGroup) error {
	if err := validateGroupRuntime(group); err != nil {
		return err
	}
	if err := validateGroupKVRuntime(group); err != nil {
		return err
	}
	return validateGroupRole(group)
}

// validateGroupRuntime checks backend topology, transport allocations, and listener conflicts.
func validateGroupRuntime(group *inferencev1alpha1.ModelGroup) error {
	if _, err := runtimeconfig.ResolveLeaderWorkerScheduling(group.Spec.Runtime.LeaderWorkerScheduling); err != nil {
		return err
	}
	if group.Spec.Runtime.SourceRevision != "" && group.Spec.Runtime.Backend != "vllm" {
		return fmt.Errorf("source bundles support the vLLM model-server only")
	}
	if group.Spec.NodeCount < 1 || group.Spec.MemberCount != group.Spec.NodeCount {
		return fmt.Errorf("model Groups require one member per node")
	}
	if group.Spec.Runtime.Backend == "vllm-omni" {
		if group.Spec.NodeCount != 1 || group.Spec.Role != inferencev1alpha1.ModelRoleAggregate {
			return fmt.Errorf("vLLM-Omni initially supports one aggregate member on one node")
		}
		if group.Spec.Runtime.Port > 65533 {
			return fmt.Errorf("vLLM-Omni model-server port must leave room for its upstream port")
		}
		if group.Spec.PDRuntime != nil || group.Spec.ECRuntime != nil || group.Spec.KVRuntime != nil || group.Spec.RDMA != nil {
			return fmt.Errorf("vLLM-Omni does not yet support split, KV, or RDMA runtime configuration")
		}
		return nil
	}
	if group.Spec.Runtime.Backend != "vllm" {
		return fmt.Errorf("inference backend %q is not supported", group.Spec.Runtime.Backend)
	}
	if err := runtimeconfig.ValidateRDMAAllocation(group.Spec.RDMA, group.Spec.Accelerator.DeviceResourceName); err != nil {
		return err
	}
	if group.Spec.PDRuntime != nil && group.Spec.PDRuntime.Protocol == "rdma" && group.Spec.RDMA == nil {
		return fmt.Errorf("P/D Groups with RDMA transport require an RDMA allocation")
	}
	if pd := group.Spec.PDRuntime; pd != nil && (group.Spec.Role == inferencev1alpha1.ModelRolePrefill || pd.Connector == inferencev1alpha1.NixlConnector) {
		lastPort := int64(pd.BootstrapPort)
		if pd.Connector == inferencev1alpha1.NixlConnector {
			lastPort += int64(group.Spec.Parallelism.DP) - 1
		}
		if lastPort > 65535 {
			return fmt.Errorf("P/D side-channel ports exceed the TCP port range")
		}
		if int64(group.Spec.Runtime.Port) >= int64(pd.BootstrapPort) && int64(group.Spec.Runtime.Port) <= lastPort {
			return fmt.Errorf("P/D side-channel ports conflict with the model-server port")
		}
		cachePort := int64(runtimeconfig.CacheObservationPort(group.Spec.Runtime.Port))
		if group.Spec.Artifacts.Cache != nil && cachePort >= int64(pd.BootstrapPort) && cachePort <= lastPort {
			return fmt.Errorf("P/D side-channel ports conflict with the runtime cache observation port")
		}
	}
	return nil
}

// validateGroupKVRuntime checks the selected cache mode and capacity before workload projection.
func validateGroupKVRuntime(group *inferencev1alpha1.ModelGroup) error {
	runtime := group.Spec.KVRuntime
	if runtime == nil {
		return nil
	}
	if (runtime.Offload == nil) == (runtime.MooncakeStore == nil) {
		return fmt.Errorf("KV runtime must select exactly one storage mode")
	}
	if runtime.Offload != nil {
		if runtime.Offload.CPUBytes < 1 {
			return fmt.Errorf("KV offload CPU bytes must be greater than zero")
		}
		return nil
	}
	return validateMooncakeStoreRuntime(group, runtime.MooncakeStore)
}

// validateMooncakeStoreRuntime checks the resolved binding and its requester buffer against Group memory.
func validateMooncakeStoreRuntime(group *inferencev1alpha1.ModelGroup, store *inferencev1alpha1.ModelGroupMooncakeStoreRuntime) error {
	if store.ProfileName == "" || store.ProfileRevision == "" || store.ConfigMapName == "" || store.ConfigMapKey == "" || store.PythonHashSeed == "" {
		return fmt.Errorf("Mooncake Store runtime config is incomplete")
	}
	if store.KVServiceUID != "" && store.RequesterBufferBytes < 1 {
		return fmt.Errorf("managed Mooncake Store requester buffer is incomplete")
	}
	if store.RequesterBufferBytes > 0 {
		if err := resourcevalidation.ValidateRequesterBufferBudget(group.Spec.Resources, store.RequesterBufferBytes); err != nil {
			return fmt.Errorf("managed Mooncake Store requester buffer: %w", err)
		}
	}
	return nil
}

// Each serving role admits a different combination of P/D and EC runtime state.
// The engine adapter validates topology when compiling the launch plan.
func validateGroupRole(group *inferencev1alpha1.ModelGroup) error {
	switch group.Spec.Role {
	case inferencev1alpha1.ModelRoleAggregate:
		return validateAggregateRole(group)
	case inferencev1alpha1.ModelRoleEncoder:
		return validateEncoderRole(group)
	case inferencev1alpha1.ModelRolePrefill:
		return validatePrefillRole(group)
	case inferencev1alpha1.ModelRoleDecode:
		return validateDecodeRole(group)
	default:
		return fmt.Errorf("ModelGroup role %q is not supported", group.Spec.Role)
	}
}

func validateAggregateRole(group *inferencev1alpha1.ModelGroup) error {
	if group.Spec.PDRuntime != nil || group.Spec.ECRuntime != nil {
		return fmt.Errorf("aggregate Groups must not have split runtime configs")
	}
	return nil
}

func validateEncoderRole(group *inferencev1alpha1.ModelGroup) error {
	if group.Spec.PDRuntime != nil {
		return fmt.Errorf("encoder Groups must not have a P/D runtime config")
	}
	if !vllm.CompleteECRuntime(group.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleProducer) {
		return fmt.Errorf("encoder Groups require a complete EC producer runtime config")
	}
	return nil
}

func validatePrefillRole(group *inferencev1alpha1.ModelGroup) error {
	if err := validatePDRuntime(group); err != nil {
		return err
	}
	if group.Spec.ECRuntime != nil && !vllm.CompleteECRuntime(group.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleConsumer) {
		return fmt.Errorf("prefill Groups require a complete EC consumer runtime config")
	}
	return nil
}

func validateDecodeRole(group *inferencev1alpha1.ModelGroup) error {
	if err := validatePDRuntime(group); err != nil {
		return err
	}
	if group.Spec.ECRuntime != nil {
		return fmt.Errorf("decode Groups must not have an EC runtime config")
	}
	return nil
}

func validatePDRuntime(group *inferencev1alpha1.ModelGroup) error {
	if group.Spec.KVRuntime != nil && group.Spec.KVRuntime.Offload != nil {
		return fmt.Errorf("P/D Groups do not support local KV offload")
	}
	runtime := group.Spec.PDRuntime
	if !vllm.CompletePDRuntime(runtime) {
		return fmt.Errorf("P/D Groups require a complete connector runtime config")
	}
	return nil
}
