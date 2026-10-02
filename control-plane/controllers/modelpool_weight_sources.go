// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Publishes fixed-weight ModelExpress sources through readiness-filtered Services.

package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"strconv"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

const (
	weightSourcePoolLabel           = "inference.foretoken.io/weight-source-pool"
	weightSourceRevisionLabel       = "inference.foretoken.io/weight-source-revision"
	weightSourceGRPCPort      int32 = 6555
	weightSourceMetadataPort  int32 = 5555
)

// modelExpressSourcesEnabled selects cohorts with stable shard placement and shared prepared files.
func modelExpressSourcesEnabled(spec inferencev1alpha1.ModelGroupSpec) bool {
	var loader string
	argument := spec.Runtime.EngineArgs["load-format"]
	return json.Unmarshal(argument.Raw, &loader) == nil && loader == "modelexpress" && spec.RDMA != nil &&
		spec.Accelerator.DeviceResourceName == "nvidia.com/gpu" && spec.Artifacts.Cache != nil &&
		spec.Artifacts.Source != inferencev1alpha1.ModelSourceLocal && spec.Parallelism.DP == 1 &&
		(spec.Parallelism.EP == nil || !spec.Parallelism.EP.EPLB)
}

func weightSourceLabels(spec inferencev1alpha1.ModelGroupSpec) map[string]string {
	return map[string]string{weightSourcePoolLabel: spec.ModelPoolRef.UID, weightSourceRevisionLabel: spec.Revision}
}

// configureWeightSources adds discovery only to serving Pod templates, never Deployment selectors.
// The upstream loader publishes tensors before engine readiness; Services exclude starting Pods.
func configureWeightSources(spec inferencev1alpha1.ModelGroupSpec, pod *corev1.PodTemplateSpec) error {
	if !modelExpressSourcesEnabled(spec) {
		return nil
	}
	gpuCount := spec.Resources.Requests.GPU.Count
	reservedPorts := []int32{spec.Runtime.Port, runtimeCacheObservationPort(spec.Runtime.Port)}
	if spec.PDRuntime != nil {
		reservedPorts = append(reservedPorts, spec.PDRuntime.BootstrapPort)
	}
	for _, base := range []int32{weightSourceGRPCPort, weightSourceMetadataPort} {
		if gpuCount < 1 || int64(base)+int64(gpuCount)-1 > 65535 {
			return fmt.Errorf("ModelExpress worker ports exceed the TCP port range")
		}
		for _, reserved := range reservedPorts {
			if reserved >= base && reserved < base+gpuCount {
				return fmt.Errorf("ModelExpress worker ports overlap runtime port %d", reserved)
			}
		}
	}
	if weightSourceMetadataPort+gpuCount > weightSourceGRPCPort {
		return fmt.Errorf("ModelExpress metadata and worker ports overlap")
	}
	if pod.Labels == nil {
		pod.Labels = make(map[string]string)
	}
	for name, value := range weightSourceLabels(spec) {
		pod.Labels[name] = value
	}
	pod.Spec.Containers[0].Env = append(pod.Spec.Containers[0].Env,
		corev1.EnvVar{Name: "MX_METADATA_BACKEND", Value: "k8s-service"},
		corev1.EnvVar{Name: "MX_P2P_METADATA", Value: "1"},
		corev1.EnvVar{Name: "MX_K8S_SERVICE_PATTERN", Value: fmt.Sprintf("mx-%s-r{rank}:%d", spec.Revision, weightSourceGRPCPort)},
		corev1.EnvVar{Name: "MX_MODEL_REVISION", Value: spec.ModelPoolRef.UID + "/" + spec.Revision},
		corev1.EnvVar{Name: "MX_WORKER_HOST", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "status.podIP"}}},
		corev1.EnvVar{Name: "MX_WORKER_GRPC_PORT", Value: strconv.Itoa(int(weightSourceGRPCPort))},
		corev1.EnvVar{Name: "MX_METADATA_PORT", Value: strconv.Itoa(int(weightSourceMetadataPort))},
		corev1.EnvVar{Name: "MX_RDMA_NIC_PIN", Value: "auto"},
	)
	return nil
}

// weightSourceIngress permits only cohort-local manifest and NIXL metadata connections.
// RDMA data uses the allocated device network, not dynamically opened Pod TCP ports.
func weightSourceIngress(spec inferencev1alpha1.ModelGroupSpec) []networkingv1.NetworkPolicyIngressRule {
	if !modelExpressSourcesEnabled(spec) {
		return nil
	}
	tcp := corev1.ProtocolTCP
	ports := make([]networkingv1.NetworkPolicyPort, 0, 2)
	for _, base := range []int32{weightSourceGRPCPort, weightSourceMetadataPort} {
		start := intstr.FromInt32(base)
		end := base + spec.Resources.Requests.GPU.Count - 1
		ports = append(ports, networkingv1.NetworkPolicyPort{Protocol: &tcp, Port: &start, EndPort: &end})
	}
	return []networkingv1.NetworkPolicyIngressRule{{From: []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{MatchLabels: weightSourceLabels(spec)}}}, Ports: ports}}
}

// reconcileWeightSources owns one Service per global shard rank for each live Pool cohort.
// With DP=1, vLLM mp places consecutive world ranks on each LWS member; ModelExpress
// listens on base+local CUDA device ID. Source Services translate the global rank to that port.
func (reconciler *ModelPoolReconciler) reconcileWeightSources(ctx context.Context, pool *inferencev1alpha1.ModelPool, target resolver.ModelGroupTemplate) error {
	groups, err := ownedModelGroups(ctx, reconciler.APIReader, pool)
	if err != nil {
		return err
	}
	cohorts := make(map[string]inferencev1alpha1.ModelGroupSpec)
	for _, group := range groups {
		cohorts[group.Spec.Revision] = group.Spec
	}
	if pool.Spec.DesiredGroups > 0 {
		cohorts[target.Revision] = target.Spec(pool, 0)
	}
	desired := make(map[string]struct{})
	for _, spec := range cohorts {
		if !modelExpressSourcesEnabled(spec) {
			continue
		}
		perNode := spec.Resources.Requests.GPU.Count
		ranks := int64(spec.Parallelism.TP) * int64(spec.Parallelism.PP) * int64(spec.Parallelism.PCP)
		for rank := int64(0); rank < ranks; rank++ {
			name := fmt.Sprintf("mx-%s-r%d", spec.Revision, rank)
			desired[name] = struct{}{}
			service := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: pool.Namespace}}
			_, err := controllerutil.CreateOrUpdate(ctx, reconciler.Client, service, func() error {
				if service.ResourceVersion != "" && !metav1.IsControlledBy(service, pool) {
					return fmt.Errorf("weight source Service %q belongs to another owner", name)
				}
				if err := controllerutil.SetControllerReference(pool, service, reconciler.Scheme()); err != nil {
					return err
				}
				service.Labels = weightSourceLabels(spec)
				selector := weightSourceLabels(spec)
				if spec.NodeCount > 1 {
					selector[lwsv1.WorkerIndexLabelKey] = strconv.FormatInt(rank/int64(perNode), 10)
				}
				service.Spec.Selector = selector
				service.Spec.PublishNotReadyAddresses = false
				service.Spec.Ports = []corev1.ServicePort{{Name: "manifest", Protocol: corev1.ProtocolTCP, Port: weightSourceGRPCPort, TargetPort: intstr.FromInt32(weightSourceGRPCPort + int32(rank%int64(perNode)))}}
				return nil
			})
			if err != nil {
				return fmt.Errorf("reconcile weight source Service: %w", err)
			}
		}
	}
	var services corev1.ServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(pool.Namespace), client.MatchingLabels{weightSourcePoolLabel: string(pool.UID)}); err != nil {
		return err
	}
	for index := range services.Items {
		service := &services.Items[index]
		if _, keep := desired[service.Name]; keep || !metav1.IsControlledBy(service, pool) {
			continue
		}
		if err := client.IgnoreNotFound(reconciler.Delete(ctx, service)); err != nil {
			return err
		}
	}
	return nil
}
