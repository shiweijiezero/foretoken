// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Builds Mooncake Master infrastructure owned by one KVService.

package controllers

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"slices"
	"strconv"
	"strings"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
)

const (
	kvServiceLabel         = "inference.foretoken.io/kvservice"
	kvMasterModeLabel      = "inference.foretoken.io/master-mode"
	kvMasterConfigRevision = "inference.foretoken.io/master-config-revision"
	masterConfigKey        = "master.yaml"
	requesterConfigKey     = "mooncake.json"
)

func kvMasterNames(service *inferencev1alpha1.KVService) (string, string, string, string) {
	// The service UID prevents retained infrastructure from a deleted KVService
	// with the same name from blocking or being adopted by its replacement.
	identity := string(service.UID)
	return kvChildName(service.Name+"-master", identity), kvChildName(service.Name+"-master-config", identity), kvChildName(service.Name+"-master-snapshots", identity), kvChildName(service.Name+"-master", identity)
}

func kvMasterHeadlessServiceName(service *inferencev1alpha1.KVService) string {
	masterName, _, _, _ := kvMasterNames(service)
	return kvChildName(masterName+"-headless", string(service.UID))
}

type kvMasterConnection struct {
	ServerAddress string
	Etcd          string
	ClusterID     string
}

// resolveKVMasterConnection is the single owner for native Master and client addressing.
func resolveKVMasterConnection(service *inferencev1alpha1.KVService) (kvMasterConnection, error) {
	rpcPort, _, _ := masterPorts(service.Spec.Master)
	_, _, _, serviceName := kvMasterNames(service)
	if service.Spec.Master.HighAvailability == nil {
		return kvMasterConnection{ServerAddress: fmt.Sprintf("%s.%s.svc:%d", serviceName, service.Namespace, rpcPort)}, nil
	}
	if service.Spec.Master.Snapshot != nil {
		return kvMasterConnection{}, fmt.Errorf("master.highAvailability cannot be combined with master.snapshot")
	}
	configuredEndpoints := slices.Clone(service.Spec.Master.HighAvailability.EtcdEndpoints)
	slices.Sort(configuredEndpoints)
	endpoints := make([]string, len(configuredEndpoints))
	for index, configured := range configuredEndpoints {
		endpoint := string(configured)
		endpoints[index] = endpoint
		if strings.TrimSpace(endpoint) != endpoint || endpoint == "" || strings.ContainsAny(endpoint, ",;") || (index > 0 && configured == configuredEndpoints[index-1]) {
			return kvMasterConnection{}, fmt.Errorf("master.highAvailability.etcdEndpoints must contain unique nonempty addresses without list separators")
		}
	}
	if len(endpoints) == 0 {
		return kvMasterConnection{}, fmt.Errorf("master.highAvailability.etcdEndpoints must not be empty")
	}
	etcd := strings.Join(endpoints, ";")
	return kvMasterConnection{ServerAddress: "etcd://" + etcd, Etcd: etcd, ClusterID: "foretoken-" + string(service.UID)}, nil
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

type kvMasterResources struct {
	connection      kvMasterConnection
	config          *corev1.ConfigMap
	requesterConfig *corev1.ConfigMap
	deployment      *appsv1.Deployment
	statefulSet     *appsv1.StatefulSet
	services        []*corev1.Service
	pvc             *corev1.PersistentVolumeClaim
}

// desiredKVMasterResources builds either the retained single-Master path or the native two-Master HA path.
func desiredKVMasterResources(service *inferencev1alpha1.KVService) (kvMasterResources, error) {
	var output kvMasterResources
	requests, limits, err := kvResources(service.Spec.Master.Resources)
	if err != nil {
		return output, err
	}
	connection, err := resolveKVMasterConnection(service)
	if err != nil {
		return output, err
	}
	masterName, configName, pvcName, serviceName := kvMasterNames(service)
	labels := map[string]string{kvServiceLabel: kvLabelValue(service.Name), "inference.foretoken.io/component": "mooncake-master"}
	podLabels := map[string]string{kvServiceLabel: labels[kvServiceLabel], "inference.foretoken.io/component": labels["inference.foretoken.io/component"]}
	if service.Spec.Master.HighAvailability != nil {
		podLabels[kvMasterModeLabel] = "ha"
	}
	rpcPort, metadataPort, metricsPort := masterPorts(service.Spec.Master)
	snapshotIntervalSeconds, err := mooncakeSnapshotIntervalSeconds(service.Spec.Master.SnapshotInterval)
	if err != nil {
		return output, err
	}
	snapshotRetentionCount := service.Spec.Master.SnapshotRetentionCount
	if snapshotRetentionCount == 0 {
		snapshotRetentionCount = 3
	}
	if snapshotRetentionCount < 1 {
		return output, fmt.Errorf("master.snapshotRetentionCount must be positive")
	}
	requesterConfig, err := desiredKVRequesterConfig(service, connection)
	if err != nil {
		return output, err
	}
	masterConfig := mooncakeMasterConfig(service, connection, rpcPort, metadataPort, metricsPort, snapshotIntervalSeconds, snapshotRetentionCount)
	config := &corev1.ConfigMap{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "ConfigMap"}, ObjectMeta: metav1.ObjectMeta{Name: configName, Namespace: service.Namespace, Labels: labels}, Data: map[string]string{masterConfigKey: masterConfig}}
	revisionInput, err := json.Marshal(struct {
		Master      inferencev1alpha1.KVMasterSpec
		DiskOffload bool
	}{service.Spec.Master, kvDiskOffloadEnabled(service)})
	if err != nil {
		return output, err
	}
	configRevision := string(revisionInput)

	volumes := []corev1.Volume{{Name: "config", VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: configName}}}}, {Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}}
	mounts := []corev1.VolumeMount{{Name: "config", MountPath: "/etc/mooncake", ReadOnly: true}, {Name: "tmp", MountPath: "/tmp"}}
	env := []corev1.EnvVar{}
	if service.Spec.Master.HighAvailability == nil {
		if service.Spec.Master.Snapshot != nil {
			volumes = append(volumes, corev1.Volume{Name: "snapshots", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: pvcName}}})
		} else {
			volumes = append(volumes, corev1.Volume{Name: "snapshots", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}})
		}
		mounts = append(mounts, corev1.VolumeMount{Name: "snapshots", MountPath: "/data/snapshots"})
		env = append(env, corev1.EnvVar{Name: "MOONCAKE_SNAPSHOT_LOCAL_PATH", Value: "/data/snapshots"})
	}
	automountToken, allowPrivilegeEscalation, readOnlyRootFilesystem := false, false, true
	container := corev1.Container{
		Name: "master", Image: service.Spec.Master.Image, ImagePullPolicy: corev1.PullIfNotPresent,
		Command: []string{"mooncake_master"}, Args: []string{"--config_path=/etc/mooncake/master.yaml", fmt.Sprintf("--enable_offload=%t", kvDiskOffloadEnabled(service))},
		Env: env, Ports: []corev1.ContainerPort{{Name: "rpc", ContainerPort: rpcPort}, {Name: "metadata", ContainerPort: metadataPort}, {Name: "metrics", ContainerPort: metricsPort}}, VolumeMounts: mounts,
		Resources: corev1.ResourceRequirements{Requests: requests, Limits: limits}, SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: &allowPrivilegeEscalation, ReadOnlyRootFilesystem: &readOnlyRootFilesystem, Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}},
	}
	podTemplate := corev1.PodTemplateSpec{ObjectMeta: metav1.ObjectMeta{Labels: podLabels, Annotations: map[string]string{kvMasterConfigRevision: configRevision}}, Spec: corev1.PodSpec{AutomountServiceAccountToken: &automountToken, Volumes: volumes, SecurityContext: &corev1.PodSecurityContext{FSGroup: service.Spec.Master.FSGroup, SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}}, Containers: []corev1.Container{container}}}

	ports := []corev1.ServicePort{{Name: "rpc", Port: rpcPort, TargetPort: intstr.FromString("rpc")}, {Name: "metrics", Port: metricsPort, TargetPort: intstr.FromString("metrics")}}
	if service.Spec.Master.HighAvailability == nil {
		ports = append(ports, corev1.ServicePort{Name: "metadata", Port: metadataPort, TargetPort: intstr.FromString("metadata")})
	}
	leaderService := &corev1.Service{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"}, ObjectMeta: metav1.ObjectMeta{Name: serviceName, Namespace: service.Namespace, Labels: labels}, Spec: corev1.ServiceSpec{Type: corev1.ServiceTypeClusterIP, Selector: podLabels, Ports: ports}}
	output.connection, output.config, output.requesterConfig, output.services = connection, config, requesterConfig, []*corev1.Service{leaderService}

	if service.Spec.Master.HighAvailability == nil {
		podTemplate.Spec.Containers[0].LivenessProbe = tcpProbe("rpc", 10)
		podTemplate.Spec.Containers[0].ReadinessProbe = tcpProbe("rpc", 5)
		one := int32(1)
		output.deployment = &appsv1.Deployment{TypeMeta: metav1.TypeMeta{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: "Deployment"}, ObjectMeta: metav1.ObjectMeta{Name: masterName, Namespace: service.Namespace, Labels: labels}, Spec: appsv1.DeploymentSpec{Replicas: &one, Strategy: appsv1.DeploymentStrategy{Type: appsv1.RecreateDeploymentStrategyType}, Selector: &metav1.LabelSelector{MatchLabels: podLabels}, Template: podTemplate}}
	} else {
		headlessName := kvMasterHeadlessServiceName(service)
		publishNotReady := true
		headless := &corev1.Service{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "Service"}, ObjectMeta: metav1.ObjectMeta{Name: headlessName, Namespace: service.Namespace, Labels: labels}, Spec: corev1.ServiceSpec{ClusterIP: corev1.ClusterIPNone, PublishNotReadyAddresses: publishNotReady, Selector: podLabels, Ports: ports}}
		output.services = append(output.services, headless)

		podTemplate.Spec.Containers[0].Env = append(podTemplate.Spec.Containers[0].Env,
			corev1.EnvVar{Name: "POD_IP", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "status.podIP"}}},
		)
		podTemplate.Spec.Containers[0].Args = append(podTemplate.Spec.Containers[0].Args, "--rpc_address=$(POD_IP)")
		podTemplate.Spec.Containers[0].LivenessProbe = httpProbe("/health", "metrics", 10)
		podTemplate.Spec.Containers[0].ReadinessProbe = httpProbe("/kv_events/status", "metrics", 5)
		podTemplate.Spec.Affinity = &corev1.Affinity{PodAntiAffinity: &corev1.PodAntiAffinity{RequiredDuringSchedulingIgnoredDuringExecution: []corev1.PodAffinityTerm{{LabelSelector: &metav1.LabelSelector{MatchLabels: podLabels}, TopologyKey: corev1.LabelHostname}}}}
		two := int32(2)
		parallel := appsv1.ParallelPodManagement
		onDelete := appsv1.OnDeleteStatefulSetStrategyType
		output.statefulSet = &appsv1.StatefulSet{TypeMeta: metav1.TypeMeta{APIVersion: appsv1.SchemeGroupVersion.String(), Kind: "StatefulSet"}, ObjectMeta: metav1.ObjectMeta{Name: masterName, Namespace: service.Namespace, Labels: labels}, Spec: appsv1.StatefulSetSpec{ServiceName: headlessName, Replicas: &two, PodManagementPolicy: parallel, UpdateStrategy: appsv1.StatefulSetUpdateStrategy{Type: onDelete}, Selector: &metav1.LabelSelector{MatchLabels: podLabels}, Template: podTemplate}}
	}

	if service.Spec.Master.Snapshot == nil {
		return output, nil
	}
	snapshotBytes, err := resourcevalidation.ParsePositiveBytes("master.snapshot.size", string(service.Spec.Master.Snapshot.Size))
	if err != nil {
		return kvMasterResources{}, err
	}
	size := *resource.NewQuantity(snapshotBytes, resource.DecimalSI)
	retention := service.Spec.Master.Snapshot.RetentionPolicy
	if retention == "" {
		retention = inferencev1alpha1.RetentionPolicyDelete
	}
	output.pvc = &corev1.PersistentVolumeClaim{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "PersistentVolumeClaim"}, ObjectMeta: metav1.ObjectMeta{Name: pvcName, Namespace: service.Namespace, Labels: labels, Annotations: map[string]string{snapshotRetentionAnnotation: string(retention)}}, Spec: corev1.PersistentVolumeClaimSpec{AccessModes: []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce}, Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: size}}}}
	if service.Spec.Master.Snapshot.StorageClassName != "" {
		output.pvc.Spec.StorageClassName = &service.Spec.Master.Snapshot.StorageClassName
	}
	return output, nil
}

// preservePVCBindingAndMetadata keeps provider-assigned PVC fields and metadata during updates.
// Callers initialize desired.Annotations; resource owners retain capacity, StorageClass, and retention decisions.
func preservePVCBindingAndMetadata(desired, existing *corev1.PersistentVolumeClaim) {
	desired.Spec.VolumeName = existing.Spec.VolumeName
	desired.Spec.VolumeMode = existing.Spec.VolumeMode
	if desired.Spec.StorageClassName == nil {
		desired.Spec.StorageClassName = existing.Spec.StorageClassName
	}
	desired.Finalizers = existing.Finalizers
	for key, value := range existing.Annotations {
		if _, present := desired.Annotations[key]; !present {
			desired.Annotations[key] = value
		}
	}
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

func mooncakeMasterConfig(service *inferencev1alpha1.KVService, connection kvMasterConnection, rpcPort, metadataPort, metricsPort int32, snapshotIntervalSeconds int64, snapshotRetentionCount int32) string {
	if service.Spec.Master.HighAvailability != nil {
		return fmt.Sprintf("rpc_port: %d\nenable_metric_reporting: true\nmetrics_port: %d\nenable_http_metadata_server: false\ncluster_id: %q\nenable_ha: true\nha_backend_type: \"etcd\"\nha_backend_connstring: %q\netcd_endpoints: %q\nenable_oplog: true\nenable_snapshot: false\nenable_snapshot_restore: false\n", rpcPort, metricsPort, connection.ClusterID, connection.Etcd, connection.Etcd)
	}
	// Snapshot retention is provider history, not a Foretoken cache TTL or eviction policy.
	return fmt.Sprintf("rpc_port: %d\nrpc_address: \"0.0.0.0\"\nenable_metric_reporting: true\nmetrics_port: %d\nenable_http_metadata_server: true\nhttp_metadata_server_host: \"0.0.0.0\"\nhttp_metadata_server_port: %d\ncluster_id: \"mooncake_cluster\"\nenable_snapshot: true\nenable_snapshot_restore: true\nsnapshot_interval_seconds: %d\nsnapshot_retention_count: %d\nsnapshot_object_store_type: \"local\"\n", rpcPort, metricsPort, metadataPort, snapshotIntervalSeconds, snapshotRetentionCount)
}

func tcpProbe(port string, periodSeconds int32) *corev1.Probe {
	return &corev1.Probe{ProbeHandler: corev1.ProbeHandler{TCPSocket: &corev1.TCPSocketAction{Port: intstr.FromString(port)}}, PeriodSeconds: periodSeconds, TimeoutSeconds: 1, FailureThreshold: 3}
}

func httpProbe(path, port string, periodSeconds int32) *corev1.Probe {
	return &corev1.Probe{ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: path, Port: intstr.FromString(port)}}, PeriodSeconds: periodSeconds, TimeoutSeconds: 1, FailureThreshold: 3}
}

// kvDiskOffloadEnabled shares the configured tier choice between Master and requesters.
func kvDiskOffloadEnabled(service *inferencev1alpha1.KVService) bool {
	for _, pool := range service.Spec.StoragePools {
		if pool.Client.Disk != nil {
			return true
		}
	}
	return false
}

// desiredKVRequesterConfig builds the per-KVService vLLM Mooncake Store configuration.
func desiredKVRequesterConfig(service *inferencev1alpha1.KVService, connection kvMasterConnection) (*corev1.ConfigMap, error) {
	bytes, err := resourcevalidation.ParsePositiveBytes("requester.localBufferSize", string(service.Spec.Requester.LocalBufferSize))
	if err != nil {
		return nil, err
	}
	// The API requires one or more pools with a shared protocol; requesters use it directly.
	protocol := service.Spec.StoragePools[0].Client.Protocol
	name := kvChildName(service.Name+"-requester-config", string(service.UID)+":"+strconv.FormatInt(service.Generation, 10))
	payload, err := json.Marshal(map[string]any{"mode": "standalone-store", "metadata_server": "P2PHANDSHAKE", "master_server_address": connection.ServerAddress, "global_segment_size": 0, "local_buffer_size": strconv.FormatInt(bytes, 10) + "B", "protocol": protocol, "device_name": "", "enable_offload": kvDiskOffloadEnabled(service)})
	if err != nil {
		return nil, err
	}
	return &corev1.ConfigMap{TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "ConfigMap"}, ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: service.Namespace, Labels: map[string]string{kvServiceLabel: kvLabelValue(service.Name), "inference.foretoken.io/component": "mooncake-requester"}}, Data: map[string]string{requesterConfigKey: string(payload)}}, nil
}

// exactPositiveBytes reads an already normalized count from controller-owned state.
func exactPositiveBytes(value inferencev1alpha1.ByteQuantity) (int64, error) {
	bytes, err := strconv.ParseInt(string(value), 10, 64)
	if err != nil || bytes < 1 {
		return 0, fmt.Errorf("must be a positive exact integer byte quantity")
	}
	return bytes, nil
}
