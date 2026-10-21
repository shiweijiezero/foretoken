// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects native connector storage and environment rules into Kubernetes workload fields.

package vllm

import (
	"strconv"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

// PDEnvironment renders the selected native transport; the workload owner publishes its bootstrap port.
func PDEnvironment(runtime *api.ModelGroupPDRuntimeConfig) []corev1.EnvVar {
	if runtime == nil {
		return nil
	}
	// Explicit RDMA requires an allocated HCA; TCP selects the connector's host staging path.
	transport := corev1.EnvVar{Name: "MC_FORCE_HCA", Value: "1"}
	if runtime.Protocol == "tcp" {
		transport = corev1.EnvVar{Name: "MC_FORCE_TCP", Value: "1"}
	}
	return []corev1.EnvVar{
		transport,
		{Name: "VLLM_MOONCAKE_BOOTSTRAP_PORT", Value: strconv.Itoa(int(runtime.BootstrapPort))},
		{Name: "VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT", Value: strconv.Itoa(int(runtime.AbortRequestTimeoutSeconds))},
	}
}

// KVStorage returns the native cache mounts and variables appended by the Group workload builder.
// The controller retains volume allocation, placement, and resource lifecycle ownership.
func KVStorage(runtime *api.ModelGroupKVRuntimeConfig) ([]corev1.Volume, []corev1.VolumeMount, []corev1.EnvVar) {
	if runtime == nil {
		return nil, nil, nil
	}
	var volumes []corev1.Volume
	var mounts []corev1.VolumeMount
	var env []corev1.EnvVar
	if runtime.Offload != nil && runtime.Offload.Filesystem {
		volumes = append(volumes, corev1.Volume{Name: "kv-offload", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "kv-offload", MountPath: FilesystemOffloadMountPath})
	}
	if store := runtime.MooncakeStore; store != nil {
		const configPath = "/etc/foretoken/mooncake/mooncake.json"
		volumes = append(volumes, corev1.Volume{Name: "mooncake-store-config", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: store.ConfigMapName}, Items: []corev1.KeyToPath{{Key: store.ConfigMapKey, Path: "mooncake.json"}}}}})
		mounts = append(mounts, corev1.VolumeMount{Name: "mooncake-store-config", MountPath: configPath, SubPath: "mooncake.json", ReadOnly: true})
		env = append(env, corev1.EnvVar{Name: "MOONCAKE_CONFIG_PATH", Value: configPath}, corev1.EnvVar{Name: "PYTHONHASHSEED", Value: store.PythonHashSeed})
	}
	return volumes, mounts, env
}

// ManagesEnvironment identifies native settings supplied after Pod projection by the vLLM runtime.
// Workload validation combines these names with the platform variables already present in the Pod.
func ManagesEnvironment(spec api.ModelGroupSpec, name string) bool {
	switch name {
	case "VLLM_USE_MODELSCOPE", "USE_PRECOMPILED_KERNEL":
		return true
	case "VLLM_HOST_IP":
		return spec.NodeCount > 1
	case "MC_FORCE_HCA", "MC_FORCE_TCP":
		return spec.PDRuntime != nil
	case "VLLM_WORKER_MULTIPROC_METHOD":
		return spec.Runtime.Profiling != nil && spec.Runtime.Profiling.Engine == "nsight"
	case "MODELSCOPE_CACHE":
		return spec.Artifacts.Source == api.ModelSourceModelScope
	default:
		return false
	}
}
