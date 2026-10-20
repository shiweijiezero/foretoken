// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves vLLM weight sharing and projects the loader's discovery, environment, and ports.
package vllm

import (
	"encoding/json"
	"fmt"
	"strconv"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

const (
	// WeightSourcePoolLabel lets the Pool controller retire only its own discovery Services.
	WeightSourcePoolLabel           = "inference.foretoken.io/weight-source-pool"
	weightSourceRevisionLabel       = "inference.foretoken.io/weight-source-revision"
	modelExpressGRPCPort      int32 = 6555
	modelExpressMetadataPort  int32 = 5555
)

// RequestsWeightSharing tells the resolver when an explicit native loader needs platform RDMA.
func (config EffectiveConfig) RequestsWeightSharing(template api.NormalizedPoolTemplate, deviceResource string) bool {
	return usesModelExpress(config.EngineArgs) && shareableWeights(template.Source, template.RuntimeCache, config.Parallelism, deviceResource)
}

// ConfigureWeightSharing selects the loader only when existing allocations and native ports permit it.
// Automatic selection never adds an RDMA scheduling requirement or overrides another explicit loader.
func (config *EffectiveConfig) ConfigureWeightSharing(template api.NormalizedPoolTemplate, deviceResource string, rdma *api.RDMAAllocation, runtimePort int32, pd *api.ModelGroupPDRuntimeConfig) {
	_, explicit := config.EngineArgs["load-format"]
	if !shareableWeights(template.Source, template.RuntimeCache, config.Parallelism, deviceResource) || rdma == nil ||
		(explicit && !usesModelExpress(config.EngineArgs)) || validateWeightSourcePorts(runtimePort, template.Resources.Requests.GPU.Count, pd) != nil {
		return
	}
	if config.EngineArgs == nil {
		config.EngineArgs = make(api.EngineArguments)
	}
	config.EngineArgs["load-format"] = apiextensionsv1.JSON{Raw: []byte(`"modelexpress"`)}
}

// WeightSourceServices projects cohort discovery for the Pool controller to own and reconcile.
// With DP=1, global ranks map to consecutive local devices on each LWS member.
func WeightSourceServices(spec api.ModelGroupSpec) []corev1.Service {
	if !weightSourcesEnabled(spec) {
		return nil
	}
	perNode := spec.Resources.Requests.GPU.Count
	ranks := int64(spec.Parallelism.TP) * int64(spec.Parallelism.PP) * int64(spec.Parallelism.PCP)
	services := make([]corev1.Service, 0, ranks)
	for rank := int64(0); rank < ranks; rank++ {
		selector := weightSourceLabels(spec)
		if spec.NodeCount > 1 {
			selector[lwsv1.WorkerIndexLabelKey] = strconv.FormatInt(rank/int64(perNode), 10)
		}
		services = append(services, corev1.Service{
			ObjectMeta: metav1.ObjectMeta{Name: fmt.Sprintf("mx-%s-r%d", spec.Revision, rank), Labels: weightSourceLabels(spec)},
			Spec: corev1.ServiceSpec{
				Selector:                 selector,
				PublishNotReadyAddresses: false,
				Ports:                    []corev1.ServicePort{{Name: "manifest", Protocol: corev1.ProtocolTCP, Port: modelExpressGRPCPort, TargetPort: intstr.FromInt32(modelExpressGRPCPort + int32(rank%int64(perNode)))}},
			},
		})
	}
	return services
}

// ConfigureWeightSources adds loader discovery to serving Pods without changing workload selectors.
// The upstream loader publishes tensors before readiness; discovery Services exclude starting Pods.
func ConfigureWeightSources(spec api.ModelGroupSpec, pod *corev1.PodTemplateSpec) error {
	if !weightSourcesEnabled(spec) {
		return nil
	}
	if err := validateWeightSourcePorts(spec.Runtime.Port, spec.Resources.Requests.GPU.Count, spec.PDRuntime); err != nil {
		return err
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
		corev1.EnvVar{Name: "MX_K8S_SERVICE_PATTERN", Value: fmt.Sprintf("mx-%s-r{rank}:%d", spec.Revision, modelExpressGRPCPort)},
		corev1.EnvVar{Name: "MX_MODEL_REVISION", Value: spec.ModelPoolRef.UID + "/" + spec.Revision},
		corev1.EnvVar{Name: "MX_WORKER_HOST", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "status.podIP"}}},
		corev1.EnvVar{Name: "MX_WORKER_GRPC_PORT", Value: strconv.Itoa(int(modelExpressGRPCPort))},
		corev1.EnvVar{Name: "MX_METADATA_PORT", Value: strconv.Itoa(int(modelExpressMetadataPort))},
		corev1.EnvVar{Name: "MX_RDMA_NIC_PIN", Value: "auto"},
	)
	return nil
}

// WeightSourceIngress describes cohort-local manifest and metadata access for the Group's NetworkPolicy.
// RDMA data uses the allocated device network, not dynamically opened Pod TCP ports.
func WeightSourceIngress(spec api.ModelGroupSpec) []networkingv1.NetworkPolicyIngressRule {
	if !weightSourcesEnabled(spec) {
		return nil
	}
	tcp := corev1.ProtocolTCP
	ports := make([]networkingv1.NetworkPolicyPort, 0, 2)
	for _, base := range []int32{modelExpressGRPCPort, modelExpressMetadataPort} {
		start := intstr.FromInt32(base)
		end := base + spec.Resources.Requests.GPU.Count - 1
		ports = append(ports, networkingv1.NetworkPolicyPort{Protocol: &tcp, Port: &start, EndPort: &end})
	}
	return []networkingv1.NetworkPolicyIngressRule{{From: []networkingv1.NetworkPolicyPeer{{PodSelector: &metav1.LabelSelector{MatchLabels: weightSourceLabels(spec)}}}, Ports: ports}}
}

// shareableWeights requires stable shard placement and prepared files shared by the cohort.
func shareableWeights(source api.ModelSource, cache *api.RuntimeCacheBinding, parallelism api.CompiledParallelism, deviceResource string) bool {
	// DP can reuse world ranks and EPLB changes expert placement after loading.
	return deviceResource == "nvidia.com/gpu" && cache != nil && source != api.ModelSourceLocal &&
		parallelism.DP == 1 && (parallelism.EP == nil || !parallelism.EP.EPLB)
}

func usesModelExpress(arguments api.EngineArguments) bool {
	var loader string
	argument := arguments["load-format"]
	return json.Unmarshal(argument.Raw, &loader) == nil && loader == "modelexpress"
}

func weightSourcesEnabled(spec api.ModelGroupSpec) bool {
	return usesModelExpress(spec.Runtime.EngineArgs) && spec.RDMA != nil &&
		shareableWeights(spec.Artifacts.Source, spec.Artifacts.Cache, spec.Parallelism, spec.Accelerator.DeviceResourceName)
}

func weightSourceLabels(spec api.ModelGroupSpec) map[string]string {
	return map[string]string{WeightSourcePoolLabel: spec.ModelPoolRef.UID, weightSourceRevisionLabel: spec.Revision}
}

// validateWeightSourcePorts keeps loader selection and Pod projection on the same native port contract.
func validateWeightSourcePorts(runtimePort, gpuCount int32, pd *api.ModelGroupPDRuntimeConfig) error {
	reservedPorts := []int32{runtimePort, runtimeconfig.CacheObservationPort(runtimePort)}
	if pd != nil {
		reservedPorts = append(reservedPorts, pd.BootstrapPort)
	}
	for _, base := range []int32{modelExpressGRPCPort, modelExpressMetadataPort} {
		if gpuCount < 1 || int64(base)+int64(gpuCount)-1 > 65535 {
			return fmt.Errorf("ModelExpress worker ports exceed the TCP port range")
		}
		for _, reserved := range reservedPorts {
			if reserved >= base && reserved < base+gpuCount {
				return fmt.Errorf("ModelExpress worker ports overlap runtime port %d", reserved)
			}
		}
	}
	if modelExpressMetadataPort+gpuCount > modelExpressGRPCPort {
		return fmt.Errorf("ModelExpress metadata and worker ports overlap")
	}
	return nil
}
