// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Builds Mooncake Master infrastructure owned by one KVService.

package controllers

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"strconv"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
)

const (
	kvServiceLabel     = "inference.foretoken.io/kvservice"
	masterConfigKey    = "master.yaml"
	requesterConfigKey = "mooncake.json"
)

func kvMasterNames(service *inferencev1alpha1.KVService) (string, string, string, string) {
	// The service UID prevents retained infrastructure from a deleted KVService
	// with the same name from blocking or being adopted by its replacement.
	identity := string(service.UID)
	return kvChildName(service.Name+"-master", identity), kvChildName(service.Name+"-master-config", identity), kvChildName(service.Name+"-master-snapshots", identity), kvChildName(service.Name+"-master", identity)
}

func kvChildName(prefix, identity string) string {
	suffix := fmt.Sprintf("-%x", sha256.Sum256([]byte(identity)))[:11]
	if len(prefix)+len(suffix) <= 63 {
		return prefix + suffix
	}
	return prefix[:63-len(suffix)] + suffix
}

func kvLabelValue(value string) string {
	if len(value) <= 63 {
		return value
	}
	return kvChildName(value, value)
}

func desiredKVMasterResources(service *inferencev1alpha1.KVService) (*corev1.ConfigMap, *corev1.ConfigMap, *appsv1.Deployment, *corev1.Service, *corev1.PersistentVolumeClaim, error) {
	requests, limits, err := kvResources(service.Spec.Master.Resources)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	masterName, configName, pvcName, serviceName := kvMasterNames(service)
	labels := map[string]string{kvServiceLabel: kvLabelValue(service.Name), "inference.foretoken.io/component": "mooncake-master"}
	rpcPort, metadataPort, metricsPort := masterPorts(service.Spec.Master)
	snapshotIntervalSeconds, err := mooncakeSnapshotIntervalSeconds(service.Spec.Master.SnapshotInterval)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	snapshotRetentionCount := service.Spec.Master.SnapshotRetentionCount
	if snapshotRetentionCount == 0 {
		snapshotRetentionCount = 3
	}
	if snapshotRetentionCount < 1 {
		return nil, nil, nil, nil, nil, fmt.Errorf("master.snapshotRetentionCount must be positive")
	}
	requesterConfig, err := desiredKVRequesterConfig(service, serviceName, rpcPort)
	if err != nil {
		return nil, nil, nil, nil, nil, err
	}
	config := &corev1.ConfigMap{
		TypeMeta:   metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "ConfigMap"},
		ObjectMeta: metav1.ObjectMeta{Name: configName, Namespace: service.Namespace, Labels: labels},
		Data:       map[string]string{masterConfigKey: mooncakeMasterConfig(rpcPort, metadataPort, metricsPort, snapshotIntervalSeconds, snapshotRetentionCount)},
	}
	volumes := []corev1.Volume{{Name: "config", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: configName}}}}, {Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}}
	if service.Spec.Master.Snapshot != nil {
		volumes = append(volumes, corev1.Volume{Name: "snapshots", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: pvcName}}})
	} else {
		// Without a PVC snapshots are intentionally ephemeral across Pod replacement.
		volumes = append(volumes, corev1.Volume{Name: "snapshots", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
	}
	automountToken, allowPrivilegeEscalation, readOnlyRootFilesystem := false, false, true
	deployment := &appsv1.Deployment{
		TypeMeta:   metav1.TypeMeta{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: "Deployment"},
		ObjectMeta: metav1.ObjectMeta{Name: masterName, Namespace: service.Namespace, Labels: labels},
		Spec: appsv1.DeploymentSpec{Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType}, Selector: &metav1.LabelSelector{MatchLabels: labels}, Template: corev1.PodTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: labels},
			Spec: corev1.PodSpec{AutomountServiceAccountToken: &automountToken, Volumes: volumes, SecurityContext: &corev1.PodSecurityContext{SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}}, Containers: []corev1.Container{{
				Name: "master", Image: service.Spec.Master.Image, ImagePullPolicy: corev1.PullIfNotPresent,
				Command: []string{"mooncake_master"}, Args: []string{"--config_path=/etc/mooncake/master.yaml", "--enable_offload"},
				Env:             []corev1.EnvVar{{Name: "MOONCAKE_SNAPSHOT_LOCAL_PATH", Value: "/data/snapshots"}},
				Ports:           []corev1.ContainerPort{{Name: "rpc", ContainerPort: rpcPort}, {Name: "metadata", ContainerPort: metadataPort}, {Name: "metrics", ContainerPort: metricsPort}},
				VolumeMounts:    []corev1.VolumeMount{{Name: "config", MountPath: "/etc/mooncake", ReadOnly: true}, {Name: "snapshots", MountPath: "/data/snapshots"}, {Name: "snapshots", MountPath: "/data/mooncake-offload"}, {Name: "tmp", MountPath: "/tmp"}},
				Resources:       corev1.ResourceRequirements{Requests: requests, Limits: limits},
				SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: &allowPrivilegeEscalation, ReadOnlyRootFilesystem: &readOnlyRootFilesystem, Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}},
				LivenessProbe:   tcpProbe("rpc", 10), ReadinessProbe: tcpProbe("rpc", 5),
			}}},
		}},
	}
	kubeService := &corev1.Service{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"}, ObjectMeta: metav1.ObjectMeta{Name: serviceName, Namespace: service.Namespace, Labels: labels}, Spec: corev1.ServiceSpec{Type: corev1.ServiceTypeClusterIP, Selector: labels, Ports: []corev1.ServicePort{{Name: "rpc", Port: rpcPort, TargetPort: intstr.FromString("rpc")}, {Name: "metadata", Port: metadataPort, TargetPort: intstr.FromString("metadata")}, {Name: "metrics", Port: metricsPort, TargetPort: intstr.FromString("metrics")}}}}
	if service.Spec.Master.Snapshot == nil {
		return config, requesterConfig, deployment, kubeService, nil, nil
	}
	size, err := resource.ParseQuantity(string(service.Spec.Master.Snapshot.Size))
	if err != nil {
		return nil, nil, nil, nil, nil, fmt.Errorf("parse snapshot PVC size: %w", err)
	}
	retention := service.Spec.Master.Snapshot.RetentionPolicy
	if retention == "" {
		retention = inferencev1alpha1.RetentionPolicyDelete
	}
	pvc := &corev1.PersistentVolumeClaim{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "PersistentVolumeClaim"}, ObjectMeta: metav1.ObjectMeta{Name: pvcName, Namespace: service.Namespace, Labels: labels, Annotations: map[string]string{snapshotRetentionAnnotation: string(retention)}}, Spec: corev1.PersistentVolumeClaimSpec{AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}, Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: size}}}}
	if service.Spec.Master.Snapshot.StorageClassName != "" {
		pvc.Spec.StorageClassName = &service.Spec.Master.Snapshot.StorageClassName
	}
	return config, requesterConfig, deployment, kubeService, pvc, nil
}

func kvResources(resources inferencev1alpha1.KVResources) (corev1.ResourceList, corev1.ResourceList, error) {
	cpu, err := resource.ParseQuantity(string(resources.Requests.CPU))
	if err != nil {
		return nil, nil, fmt.Errorf("parse KV CPU request: %w", err)
	}
	memory, err := resource.ParseQuantity(string(resources.Requests.Memory))
	if err != nil {
		return nil, nil, fmt.Errorf("parse KV memory request: %w", err)
	}
	requests, limits := corev1.ResourceList{corev1.ResourceCPU: cpu, corev1.ResourceMemory: memory}, corev1.ResourceList{}
	if resources.Limits == nil {
		return requests, limits, nil
	}
	if resources.Limits.CPU != nil {
		value, err := resource.ParseQuantity(string(*resources.Limits.CPU))
		if err != nil {
			return nil, nil, err
		}
		limits[corev1.ResourceCPU] = value
	}
	if resources.Limits.Memory != nil {
		value, err := resource.ParseQuantity(string(*resources.Limits.Memory))
		if err != nil {
			return nil, nil, err
		}
		limits[corev1.ResourceMemory] = value
	}
	return requests, limits, nil
}

func masterPorts(master inferencev1alpha1.KVMasterSpec) (int32, int32, int32) {
	rpc, metadata, metrics := master.RPCPort, master.MetadataPort, master.MetricsPort
	if rpc == 0 {
		rpc = 50051
	}
	if metadata == 0 {
		metadata = 8080
	}
	if metrics == 0 {
		metrics = 9003
	}
	return rpc, metadata, metrics
}

func mooncakeSnapshotIntervalSeconds(value inferencev1alpha1.Duration) (int64, error) {
	if value == "" {
		value = "60s"
	}
	interval, err := time.ParseDuration(string(value))
	if err != nil || interval <= 0 || interval%time.Second != 0 {
		return 0, fmt.Errorf("master.snapshotInterval must be a positive whole number of seconds")
	}
	return int64(interval / time.Second), nil
}

func mooncakeMasterConfig(rpcPort, metadataPort, metricsPort int32, snapshotIntervalSeconds int64, snapshotRetentionCount int32) string {
	// Field names follow llm-d v0.8.0's Master ConfigMap. Snapshot retention is
	// provider history, not a Foretoken cache TTL or eviction policy.
	return fmt.Sprintf("rpc_port: %d\nrpc_address: \"0.0.0.0\"\nenable_metric_reporting: true\nmetrics_port: %d\nenable_http_metadata_server: true\nhttp_metadata_server_host: \"0.0.0.0\"\nhttp_metadata_server_port: %d\ncluster_id: \"mooncake_cluster\"\nroot_fs_dir: \"/data/mooncake-offload\"\nenable_snapshot: true\nenable_snapshot_restore: true\nsnapshot_interval_seconds: %d\nsnapshot_retention_count: %d\nsnapshot_object_store_type: \"local\"\n", rpcPort, metricsPort, metadataPort, snapshotIntervalSeconds, snapshotRetentionCount)
}

func tcpProbe(port string, periodSeconds int32) *corev1.Probe {
	return &corev1.Probe{ProbeHandler: corev1.ProbeHandler{TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromString(port)}}, PeriodSeconds: periodSeconds, TimeoutSeconds: 1, FailureThreshold: 3}
}

// desiredKVRequesterConfig builds the per-KVService vLLM Mooncake Store configuration.
func desiredKVRequesterConfig(service *inferencev1alpha1.KVService, masterService string, rpcPort int32) (*corev1.ConfigMap, error) {
	bytes, err := exactPositiveBytes(service.Spec.Requester.LocalBufferSize)
	if err != nil {
		return nil, fmt.Errorf("parse requester.localBufferSize: %w", err)
	}
	name := kvChildName(service.Name+"-requester-config", string(service.UID)+":"+strconv.FormatInt(service.Generation, 10))
	endpoint := fmt.Sprintf("%s.%s.svc:%d", masterService, service.Namespace, rpcPort)
	payload, err := json.Marshal(map[string]any{"mode": "standalone-store", "metadata_server": "P2PHANDSHAKE", "master_server_address": endpoint, "global_segment_size": 0, "local_buffer_size": strconv.FormatInt(bytes, 10) + "B", "protocol": "rdma", "device_name": "", "enable_offload": true})
	if err != nil {
		return nil, err
	}
	return &corev1.ConfigMap{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "ConfigMap"}, ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: service.Namespace, Labels: map[string]string{kvServiceLabel: kvLabelValue(service.Name), "inference.foretoken.io/component": "mooncake-requester"}}, Data: map[string]string{requesterConfigKey: string(payload)}}, nil
}

func exactPositiveBytes(value inferencev1alpha1.ByteQuantity) (int64, error) {
	bytes, err := strconv.ParseInt(string(value), 10, 64)
	if err != nil || bytes < 1 {
		return 0, fmt.Errorf("must be a positive exact integer byte quantity")
	}
	return bytes, nil
}
