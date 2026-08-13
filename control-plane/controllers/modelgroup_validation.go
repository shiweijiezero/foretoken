// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
)

func validateGroupProfile(group *inferencev1alpha1.ModelGroup) error {
	if err := validateGroupRuntime(group); err != nil {
		return err
	}
	if err := validateGroupKVRuntime(group); err != nil {
		return err
	}
	return validateGroupRole(group)
}

func validateGroupRuntime(group *inferencev1alpha1.ModelGroup) error {
	if group.Spec.NodeCount != 1 || group.Spec.MemberCount != 1 || group.Spec.Runtime.Backend != "vllm" {
		return fmt.Errorf("only single-member vLLM Groups are currently supported")
	}
	return nil
}

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
	if !completeECRuntime(group.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleProducer) {
		return fmt.Errorf("encoder Groups require a complete EC producer runtime config")
	}
	return validateEPDParallelism(group.Spec.Parallelism)
}

func validatePrefillRole(group *inferencev1alpha1.ModelGroup) error {
	if err := validatePDRuntime(group); err != nil {
		return err
	}
	if group.Spec.ECRuntime != nil && !completeECRuntime(group.Spec.ECRuntime, inferencev1alpha1.ECTransferRoleConsumer) {
		return fmt.Errorf("prefill Groups require a complete EC consumer runtime config")
	}
	return validateEPDParallelism(group.Spec.Parallelism)
}

func validateDecodeRole(group *inferencev1alpha1.ModelGroup) error {
	if err := validatePDRuntime(group); err != nil {
		return err
	}
	if group.Spec.ECRuntime != nil {
		return fmt.Errorf("decode Groups must not have an EC runtime config")
	}
	return validateEPDParallelism(group.Spec.Parallelism)
}

func validatePDRuntime(group *inferencev1alpha1.ModelGroup) error {
	if group.Spec.KVRuntime != nil && group.Spec.KVRuntime.Offload != nil {
		return fmt.Errorf("P/D Groups do not support local KV offload")
	}
	runtime := group.Spec.PDRuntime
	if !completePDRuntime(runtime) || runtime.BootstrapPort > 65535 {
		return fmt.Errorf("P/D Groups require a complete Mooncake runtime config")
	}
	return nil
}

func completeECRuntime(runtime *inferencev1alpha1.ModelGroupECRuntimeConfig, role inferencev1alpha1.ECTransferRole) bool {
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

func validateEPDParallelism(parallelism inferencev1alpha1.CompiledParallelism) error {
	if parallelism.TP != 1 || parallelism.PP != 1 || parallelism.DP != 1 || parallelism.PCP != 1 || parallelism.DCP != 1 || parallelism.EP != nil {
		return fmt.Errorf("E/P/D Groups currently require TP=PP=DP=PCP=DCP=1 with EP disabled")
	}
	return nil
}
