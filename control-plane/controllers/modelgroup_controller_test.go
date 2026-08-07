// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Tests single-member ModelGroup workload materialization and isolation.

package controllers

import (
	"context"
	"strings"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/managedfields"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func reconcileModelGroup(t *testing.T, ctx context.Context, reconciler *ModelGroupReconciler, request ctrl.Request) {
	t.Helper()
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
}

func TestModelGroupReconcileMaterializesIsolatedDeployment(t *testing.T) {
	ctx := context.Background()
	pool, group := testModelGroup("demo-group")
	reconciler, kubeClient := testGroupReconciler(t, pool, group)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
	reconcileModelGroup(t, ctx, reconciler, request)
	reconcileModelGroup(t, ctx, reconciler, request)

	deployment := new(appsv1.Deployment)
	if err := kubeClient.Get(ctx, request.NamespacedName, deployment); err != nil {
		t.Fatal(err)
	}
	if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas != 1 || deployment.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType {
		t.Fatalf("Deployment = %#v", deployment.Spec)
	}
	podSpec := deployment.Spec.Template.Spec
	if podSpec.AutomountServiceAccountToken == nil || *podSpec.AutomountServiceAccountToken {
		t.Fatal("runtime Pod automounts a service-account token")
	}
	if podSpec.NodeSelector["nvidia.com/gpu.product"] != "NVIDIA-H100-80GB-HBM3" {
		t.Fatalf("node selector = %#v", podSpec.NodeSelector)
	}
	container := podSpec.Containers[0]
	if container.Image != "vllm:test" || container.Command[0] != "foretoken-model-server" || len(container.Ports) != 1 || container.Ports[0].ContainerPort != group.Spec.Runtime.Port || container.ReadinessProbe == nil || container.LivenessProbe == nil || container.StartupProbe == nil {
		t.Fatalf("runtime container = %#v", container)
	}
	if len(container.Args) != 0 {
		t.Fatalf("runtime args = %#v", container.Args)
	}
	if container.Env[0].Name != "FORETOKEN_VLLM_LAUNCH_PLAN" || !strings.Contains(container.Env[0].Value, `"version":1`) || !strings.Contains(container.Env[0].Value, `--max-model-len=32768`) {
		t.Fatalf("launch plan = %#v", container.Env[0])
	}
	if len(container.Env) != 9 || container.Env[1].Name != "FORETOKEN_INTERNAL_LISTEN" || container.Env[1].Value != "0.0.0.0:9000" || container.Env[3].Name != "HF_HOME" || container.Env[6].Name != "FORETOKEN_KV_INDEX_KEY_PATH" || container.Env[7].Name != "FORETOKEN_KV_SCOPE_ID" {
		t.Fatalf("runtime transport environment = %#v", container.Env)
	}
	if podSpec.TerminationGracePeriodSeconds == nil || *podSpec.TerminationGracePeriodSeconds != 125 {
		t.Fatalf("termination grace = %#v", podSpec.TerminationGracePeriodSeconds)
	}
	if podSpec.SecurityContext == nil || podSpec.SecurityContext.RunAsNonRoot == nil || !*podSpec.SecurityContext.RunAsNonRoot || container.SecurityContext == nil || container.SecurityContext.AllowPrivilegeEscalation == nil || *container.SecurityContext.AllowPrivilegeEscalation || len(container.SecurityContext.Capabilities.Drop) != 1 {
		t.Fatalf("runtime security context = pod %#v container %#v", podSpec.SecurityContext, container.SecurityContext)
	}
	if len(container.VolumeMounts) != 4 || container.VolumeMounts[0].MountPath != "/var/cache/foretoken" || container.VolumeMounts[2].MountPath != "/dev/shm" || container.VolumeMounts[3].MountPath != "/etc/foretoken/kv-indexer" {
		t.Fatalf("runtime writable mounts = %#v", container.VolumeMounts)
	}
	if _, exists := container.Resources.Limits[corev1.ResourceName("nvidia.com/gpu")]; !exists {
		t.Fatalf("accelerator limits = %#v", container.Resources.Limits)
	}

	service := new(corev1.Service)
	if err := kubeClient.Get(ctx, request.NamespacedName, service); err != nil {
		t.Fatal(err)
	}
	if !metav1.IsControlledBy(service, group) || service.Spec.Selector[modelGroupLabel] != group.Name || service.Spec.Ports[0].TargetPort.String() != "model-server" {
		t.Fatalf("Service = %#v", service.Spec)
	}

	policy := new(networkingv1.NetworkPolicy)
	if err := kubeClient.Get(ctx, request.NamespacedName, policy); err != nil {
		t.Fatal(err)
	}
	if !metav1.IsControlledBy(policy, group) || len(policy.Spec.Ingress) != 1 || len(policy.Spec.PolicyTypes) != 1 || policy.Spec.PolicyTypes[0] != networkingv1.PolicyTypeIngress {
		t.Fatalf("NetworkPolicy = %#v", policy.Spec)
	}
	peers := policy.Spec.Ingress[0].From
	if len(peers) != 2 || peers[0].PodSelector == nil || peers[0].PodSelector.MatchExpressions[0].Key != frontendServiceLabel || peers[1].NamespaceSelector.MatchLabels["kubernetes.io/metadata.name"] != "foretoken-system" || peers[1].PodSelector.MatchLabels[controlPlanePodLabel] != controlPlanePodLabelValue || policy.Spec.Ingress[0].Ports[0].Port.String() != "model-server" {
		t.Fatalf("NetworkPolicy model-server ingress = %#v", policy.Spec.Ingress)
	}

	policy.Spec.Ingress = nil
	if err := kubeClient.Update(ctx, policy); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, policy); err != nil {
		t.Fatal(err)
	}
	if len(policy.Spec.Ingress) != 1 || policy.Spec.Ingress[0].Ports[0].Port.String() != "model-server" {
		t.Fatalf("NetworkPolicy drift was not repaired: %#v", policy.Spec)
	}

	// Server-side apply repairs owned fields without claiming admission-owned defaults.
	if err := kubeClient.Get(ctx, request.NamespacedName, deployment); err != nil {
		t.Fatal(err)
	}
	zero := int32(0)
	deployment.Spec.Replicas = &zero
	if err := kubeClient.Update(ctx, deployment); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, deployment); err != nil {
		t.Fatal(err)
	}
	if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas != 1 {
		t.Fatalf("Deployment drift was not repaired: %#v", deployment.Spec.Replicas)
	}

	current := new(inferencev1alpha1.ModelGroup)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionWorkloadMaterialized); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("WorkloadMaterialized condition = %#v", condition)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelGroupReconcileMaterializesMooncakePrefill(t *testing.T) {
	ctx := context.Background()
	pool, group := testModelGroup("prefill-group")
	group.Spec.Role = inferencev1alpha1.ModelRolePrefill
	group.Spec.PDRuntime = &inferencev1alpha1.ModelGroupPDRuntimeConfig{
		ProfileName: "cluster-pd", ProfileRevision: "release-A", Connector: "MooncakeConnector", Protocol: "rdma", BootstrapPort: 29001, AbortRequestTimeoutSeconds: 30,
	}
	reconciler, kubeClient := testGroupReconciler(t, pool, group)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
	reconcileModelGroup(t, ctx, reconciler, request)
	reconcileModelGroup(t, ctx, reconciler, request)
	deployment := new(appsv1.Deployment)
	if err := kubeClient.Get(ctx, request.NamespacedName, deployment); err != nil {
		t.Fatal(err)
	}
	container := deployment.Spec.Template.Spec.Containers[0]
	if len(container.Args) != 0 || !strings.Contains(container.Env[0].Value, `"kind":"pd"`) || len(container.Ports) != 2 || container.Ports[1].Name != "mooncake-bootstrap" || container.Env[9].Name != "VLLM_MOONCAKE_BOOTSTRAP_PORT" || container.Env[10].Name != "VLLM_MOONCAKE_ABORT_REQUEST_TIMEOUT" {
		t.Fatalf("P/D container = %#v", container)
	}
	service := new(corev1.Service)
	if err := kubeClient.Get(ctx, request.NamespacedName, service); err != nil {
		t.Fatal(err)
	}
	if len(service.Spec.Ports) != 2 || service.Spec.Ports[1].Name != "mooncake-bootstrap" || service.Spec.Ports[1].Port != 29001 {
		t.Fatalf("P/D Service = %#v", service.Spec)
	}
	policy := new(networkingv1.NetworkPolicy)
	if err := kubeClient.Get(ctx, request.NamespacedName, policy); err != nil {
		t.Fatal(err)
	}
	if len(policy.Spec.Ingress) != 3 {
		t.Fatalf("P/D NetworkPolicy = %#v", policy.Spec)
	}
	decodeIngress := policy.Spec.Ingress[1]
	frontendBootstrapIngress := policy.Spec.Ingress[2]
	if len(decodeIngress.From) != 1 || decodeIngress.From[0].PodSelector.MatchLabels[modelGroupRoleLabel] != string(inferencev1alpha1.ModelRoleDecode) || decodeIngress.From[0].PodSelector.MatchLabels[modelGroupPDDomainLabel] != deployment.Spec.Template.Labels[modelGroupPDDomainLabel] || len(decodeIngress.Ports) != 0 || len(frontendBootstrapIngress.From) != 1 || frontendBootstrapIngress.From[0].PodSelector.MatchExpressions[0].Key != frontendServiceLabel || len(frontendBootstrapIngress.Ports) != 1 || frontendBootstrapIngress.Ports[0].Port.IntVal != 29001 {
		t.Fatalf("P/D NetworkPolicy = %#v", policy.Spec)
	}
}

func TestModelGroupRejectsPDWithoutRuntimeConfig(t *testing.T) {
	ctx := context.Background()
	pool, group := testModelGroup("invalid-pd-group")
	group.Spec.Role = inferencev1alpha1.ModelRoleDecode
	reconciler, kubeClient := testGroupReconciler(t, pool, group)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
	reconcileModelGroup(t, ctx, reconciler, request)
	current := new(inferencev1alpha1.ModelGroup)
	if err := kubeClient.Get(ctx, client.ObjectKeyFromObject(group), current); err != nil {
		t.Fatal(err)
	}
	if current.Status.Phase != inferencev1alpha1.ModelGroupPhaseFailed || len(current.Finalizers) != 0 {
		t.Fatalf("P/D without runtime status = %#v, finalizers = %#v", current.Status, current.Finalizers)
	}
}

func TestModelGroupRejectsPDOutsideSingleRankProfile(t *testing.T) {
	_, group := testModelGroup("invalid-pd-parallelism")
	group.Spec.Role = inferencev1alpha1.ModelRolePrefill
	group.Spec.PDRuntime = &inferencev1alpha1.ModelGroupPDRuntimeConfig{
		ProfileName: "cluster-pd", ProfileRevision: "release-A", Connector: "MooncakeConnector", Protocol: "rdma", BootstrapPort: 29001, AbortRequestTimeoutSeconds: 30,
	}
	group.Spec.Parallelism.DP = 2

	if err := validateGroupProfile(group); err == nil {
		t.Fatal("P/D Group with DP=2 was accepted")
	}
}

func TestModelGroupReportsInsufficientCapacityOnlyForUnschedulablePod(t *testing.T) {
	ctx := context.Background()
	pool, group := testModelGroup("capacity-group")
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "capacity-group-pod", Namespace: group.Namespace, Labels: modelGroupLabels(group)},
		Status: corev1.PodStatus{Conditions: []corev1.PodCondition{{
			Type: corev1.PodScheduled, Status: corev1.ConditionFalse, Reason: corev1.PodReasonUnschedulable,
		}}},
	}
	reconciler, kubeClient := testGroupReconciler(t, pool, group, pod)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
	reconcileModelGroup(t, ctx, reconciler, request)
	reconcileModelGroup(t, ctx, reconciler, request)

	current := new(inferencev1alpha1.ModelGroup)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	condition := meta.FindStatusCondition(current.Status.Conditions, conditionSchedulingCapacity)
	if condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "InsufficientCapacity" {
		t.Fatalf("SchedulingCapacity condition = %#v", condition)
	}

	nonUnschedulablePod := pod.DeepCopy()
	nonUnschedulablePod.Name = "waiting-group-pod"
	nonUnschedulablePod.Status.Conditions[0].Reason = "SchedulingGated"
	waitingReconciler, _ := testGroupReconciler(t, pool, group, nonUnschedulablePod)
	state, err := waitingReconciler.schedulingCapacity(ctx, group)
	if err != nil {
		t.Fatal(err)
	}
	if state.status != metav1.ConditionUnknown || state.reason != "WaitingForScheduling" {
		t.Fatalf("non-Unschedulable scheduling state = %#v", state)
	}
}

func TestModelGroupBecomesReadyOnlyAfterModelServerReadiness(t *testing.T) {
	ctx := context.Background()
	pool, group := testModelGroup("ready-group")
	reconciler, kubeClient := testGroupReconciler(t, pool, group)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
	reconcileModelGroup(t, ctx, reconciler, request)
	reconcileModelGroup(t, ctx, reconciler, request)

	deployment := new(appsv1.Deployment)
	if err := kubeClient.Get(ctx, request.NamespacedName, deployment); err != nil {
		t.Fatal(err)
	}
	deployment.Status.ObservedGeneration = deployment.Generation
	deployment.Status.AvailableReplicas = 1
	deployment.Status.Conditions = []appsv1.DeploymentCondition{{Type: appsv1.DeploymentAvailable, Status: corev1.ConditionTrue}}
	if err := kubeClient.Status().Update(ctx, deployment); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}

	current := new(inferencev1alpha1.ModelGroup)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.Phase != inferencev1alpha1.ModelGroupPhaseReady || current.Status.ReadyMembers != 1 {
		t.Fatalf("ready status = %#v", current.Status)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelGroupDoesNotAdoptConflictingDeployment(t *testing.T) {
	pool, group := testModelGroup("demo-group")
	deployment := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Name: group.Name, Namespace: group.Namespace}}
	reconciler, _ := testGroupReconciler(t, pool, group, deployment)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
	reconcileModelGroup(t, context.Background(), reconciler, request)
	if _, err := reconciler.Reconcile(context.Background(), request); err == nil {
		t.Fatal("conflicting Deployment was adopted")
	}
}

func TestModelGroupNetworkPolicyDoesNotAdoptForeignOwner(t *testing.T) {
	pool, group := testModelGroup("demo-group")
	policy := &networkingv1.NetworkPolicy{ObjectMeta: metav1.ObjectMeta{
		Name:      group.Name,
		Namespace: group.Namespace,
		OwnerReferences: []metav1.OwnerReference{{
			APIVersion: "example.io/v1",
			Kind:       "ForeignOwner",
			Name:       "foreign",
			UID:        "foreign-uid",
			Controller: ptr(true),
		}},
	}}
	reconciler, _ := testGroupReconciler(t, pool, group, policy)
	if err := reconciler.reconcileNetworkPolicy(context.Background(), group); err == nil {
		t.Fatal("conflicting NetworkPolicy was adopted")
	}
}

func containsArgument(args []string, expected string) bool {
	for _, arg := range args {
		if arg == expected {
			return true
		}
	}
	return false
}

func testGroupReconciler(t *testing.T, objects ...client.Object) (*ModelGroupReconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := inferencev1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := appsv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := networkingv1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithTypeConverters(managedfields.NewDeducedTypeConverter()).
		WithStatusSubresource(&inferencev1alpha1.ModelGroup{}, &appsv1.Deployment{}).
		WithObjects(objects...).
		Build()
	return &ModelGroupReconciler{Client: kubeClient, ControlPlaneNamespace: "foretoken-system"}, kubeClient
}

func testModelGroup(name string) (*inferencev1alpha1.ModelPool, *inferencev1alpha1.ModelGroup) {
	pool := &inferencev1alpha1.ModelPool{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelPool"},
		ObjectMeta: metav1.ObjectMeta{Name: "demo", Namespace: "default", UID: "demo-uid"},
	}
	group := &inferencev1alpha1.ModelGroup{
		TypeMeta: metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelGroup"},
		ObjectMeta: metav1.ObjectMeta{
			Name:      name,
			Namespace: "default",
			UID:       types.UID(name + "-uid"),
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: pool.APIVersion,
				Kind:       pool.Kind,
				Name:       pool.Name,
				UID:        pool.UID,
				Controller: ptr(true),
			}},
		},
		Spec: inferencev1alpha1.ModelGroupSpec{
			ModelPoolRef: inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)},
			Revision:     "r-example",
			Ordinal:      0,
			Role:         inferencev1alpha1.ModelRoleAggregate,
			Artifacts: inferencev1alpha1.ModelGroupArtifacts{
				Model:             "Qwen/Qwen3-0.6B",
				ModelRevision:     "model-revision",
				Tokenizer:         "Qwen/Qwen3-0.6B",
				TokenizerRevision: "model-revision",
			},
			Runtime: inferencev1alpha1.ModelGroupRuntime{
				Backend: "vllm",
				Image:   "vllm:test",
				Port:    9000,
				Args:    []inferencev1alpha1.BackendArg{"--max-model-len=32768"},
			},
			Resources: inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
				ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
				GPU:                     inferencev1alpha1.GPURequest{Type: "nvidia-h100-80gb", Count: 1},
			}},
			Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
			NodeCount:   1,
			MemberCount: 1,
			Parallelism: inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1},
			Accelerator: inferencev1alpha1.ModelGroupAccelerator{
				DeviceResourceName: "nvidia.com/gpu",
				NodeSelector:       map[string]string{"nvidia.com/gpu.product": "NVIDIA-H100-80GB-HBM3"},
			},
		},
	}
	return pool, group
}

func TestDesiredDeploymentMountsKVRuntime(t *testing.T) {
	_, group := testModelGroup("kv-group")
	group.Spec.KVRuntime = &inferencev1alpha1.ModelGroupKVRuntimeConfig{Offload: &inferencev1alpha1.ModelGroupKVOffloadRuntime{CPUBytes: 1024, Filesystem: true}}
	deployment, err := desiredDeployment(group)
	if err != nil || !hasMount(deployment.Spec.Template.Spec.Containers[0].VolumeMounts, "/var/lib/foretoken/kv-offload") {
		t.Fatalf("FS deployment = %#v, err = %v", deployment, err)
	}
	group.Spec.KVRuntime = &inferencev1alpha1.ModelGroupKVRuntimeConfig{MooncakeStore: &inferencev1alpha1.ModelGroupMooncakeStoreRuntime{ProfileName: "store", ProfileRevision: "r1", ConfigMapName: "store-config", ConfigMapKey: "config.json", PythonHashSeed: "0"}}
	deployment, err = desiredDeployment(group)
	container := deployment.Spec.Template.Spec.Containers[0]
	if err != nil || !hasMount(container.VolumeMounts, "/etc/foretoken/mooncake/mooncake.json") || !hasEnv(container.Env, "MOONCAKE_CONFIG_PATH") || !hasEnv(container.Env, "PYTHONHASHSEED") {
		t.Fatalf("Store deployment = %#v, err = %v", deployment, err)
	}
}

func hasMount(mounts []corev1.VolumeMount, path string) bool {
	for _, mount := range mounts {
		if mount.MountPath == path {
			return true
		}
	}
	return false
}
func hasEnv(env []corev1.EnvVar, name string) bool {
	for _, item := range env {
		if item.Name == name {
			return true
		}
	}
	return false
}
