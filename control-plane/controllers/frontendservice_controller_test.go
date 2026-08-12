// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Tests FrontendService convergence, ownership protection, and route conditions.

package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"slices"
	"strings"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"
)

func TestFrontendServiceReconcileConvergesDeploymentDrift(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	reconciler, kubeClient := testFrontendReconciler(t, frontend)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(frontend)}

	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	deployment := new(appsv1.Deployment)
	if err := kubeClient.Get(ctx, request.NamespacedName, deployment); err != nil {
		t.Fatal(err)
	}
	deployment.Spec.Template.Spec.Containers[0].Image = "unapproved:image"
	if err := kubeClient.Update(ctx, deployment); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, deployment); err != nil {
		t.Fatal(err)
	}
	if got := deployment.Spec.Template.Spec.Containers[0].Image; got != "registry.example/frontend:v1" {
		t.Fatalf("Deployment image after drift reconciliation = %q", got)
	}
	if !metav1.IsControlledBy(deployment, frontend) {
		t.Fatal("Deployment is not controlled by FrontendService")
	}
	podSpec := deployment.Spec.Template.Spec
	if podSpec.SecurityContext == nil || podSpec.SecurityContext.FSGroup == nil || *podSpec.SecurityContext.FSGroup != 65532 {
		t.Fatalf("frontend Pod security context = %#v", podSpec.SecurityContext)
	}
	container := podSpec.Containers[0]
	env := make(map[string]string, len(container.Env))
	for _, variable := range container.Env {
		env[variable.Name] = variable.Value
	}
	mounts := make(map[string]bool, len(container.VolumeMounts))
	for _, mount := range container.VolumeMounts {
		mounts[mount.MountPath] = true
	}
	_, hasKVKey := env["FORETOKEN_KV_INDEX_KEY_PATH"]
	if !mounts["/var/cache/foretoken"] || !mounts["/etc/foretoken/kv-indexer"] || env["HF_HOME"] != "/var/cache/foretoken/huggingface" || env["FORETOKEN_STREAM_IDLE_SECONDS"] != "300" || !hasKVKey || env["FORETOKEN_ROUTER_FILTER"] != "allow_all" || env["FORETOKEN_ROUTER_SCORER"] != "kv_least_loaded" || env["FORETOKEN_ROUTER_PICKER"] != "round_robin" {
		t.Fatalf("frontend tokenizer cache contract = env %#v mounts %#v", container.Env, container.VolumeMounts)
	}

	service := new(corev1.Service)
	if err := kubeClient.Get(ctx, request.NamespacedName, service); err != nil {
		t.Fatal(err)
	}
	if service.Spec.Type != corev1.ServiceTypeClusterIP || service.Spec.Selector[frontendServiceLabel] != frontend.Name || service.Spec.Ports[0].TargetPort.String() != "http" {
		t.Fatalf("Service does not select the frontend workload: %#v", service.Spec)
	}
	route := new(gatewayv1.HTTPRoute)
	if err := kubeClient.Get(ctx, request.NamespacedName, route); err != nil {
		t.Fatal(err)
	}
	backendRefs := route.Spec.Rules[0].BackendRefs
	if len(backendRefs) != 1 || string(backendRefs[0].Name) != frontend.Name {
		t.Fatalf("HTTPRoute backend = %#v", backendRefs)
	}
	matches := route.Spec.Rules[0].Matches
	if len(matches) != 3 || *matches[0].Path.Value != "/v1" || *matches[1].Path.Value != "/tokenize" || *matches[2].Path.Value != "/detokenize" {
		t.Fatalf("HTTPRoute public paths = %#v", matches)
	}
	current := new(inferencev1alpha1.FrontendService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if ready := meta.FindStatusCondition(current.Status.Conditions, conditionReady); ready == nil || ready.Status != metav1.ConditionFalse || ready.Reason != "WorkloadUnavailable" {
		t.Fatalf("Ready condition = %#v", ready)
	}
}

func TestServingSnapshotPublishesLogicalScalingTargetAtZeroGroups(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("scale-zero")
	service := testModelService("model", 1)
	service.Spec.Autoscaling = &inferencev1alpha1.ModelAutoscalingConfig{
		Algorithm: inferencev1alpha1.AutoscalingAlgorithmQueue,
		MinGroups: ptr(int32(0)),
		MaxGroups: ptr(int32(2)),
	}
	service.Status.ObservedGeneration = service.Generation
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionIntentCompiled, Status: metav1.ConditionTrue, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionPoolsMaterialized, Status: metav1.ConditionTrue, ObservedGeneration: service.Generation})
	pool := readyPool(service, "default", inferencev1alpha1.ModelRoleAggregate, 0, "r1")
	pool.Spec.Template.Model = service.Spec.Model
	pool.Spec.Template.ModelRevision = "r1"
	pool.Spec.Template.Tokenizer = service.Spec.Model
	pool.Spec.Template.TokenizerRevision = "r1"
	pool.Spec.Template.Features = inferencev1alpha1.ModelFeatures{}
	reconciler, kubeClient := testFrontendReconciler(t, frontend, service, pool)

	installed, err := reconciler.reconcileServingSnapshot(ctx, frontend)
	if err != nil || !installed {
		t.Fatalf("reconcileServingSnapshot() = %v, %v", installed, err)
	}
	config := new(corev1.ConfigMap)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: frontendServingConfigMapName(frontend)}, config); err != nil {
		t.Fatal(err)
	}
	var snapshot servingSnapshot
	if err := json.Unmarshal([]byte(config.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if len(snapshot.Groups) != 0 || len(snapshot.Models) != 1 || len(snapshot.Models[0].Targets) != 1 {
		t.Fatalf("logical zero snapshot = %#v", snapshot)
	}
	target := snapshot.Models[0].Targets[0]
	if target.Kind != "Pool" || target.UID != string(pool.UID) || target.ServiceUID != string(service.UID) {
		t.Fatalf("logical scaling target = %#v", target)
	}
}

func TestFrontendRoutingConfigMapNameFitsDNSLabel(t *testing.T) {
	frontend := testFrontendService(strings.Repeat("a", 63))
	name := frontendServingConfigMapName(frontend)
	if len(name) > 63 || name == frontend.Name+"-serving" || name != frontendServingConfigMapName(frontend) {
		t.Fatalf("serving snapshot ConfigMap name = %q", name)
	}
	deployment, _, _, err := frontendDesiredResources(frontend, FrontendRuntimeProfile{Image: "registry.example/frontend:v1", Port: 8080, Gateway: GatewayParent{Name: "public"}})
	if err != nil {
		t.Fatal(err)
	}
	if got := deployment.Spec.Template.Spec.Volumes[0].ConfigMap.Name; got != name {
		t.Fatalf("Deployment serving snapshot ConfigMap = %q, want %q", got, name)
	}
}

func TestFrontendServiceReplicasDefaultsAndPreservesZero(t *testing.T) {
	frontend := testFrontendService("chat")
	profile := FrontendRuntimeProfile{Image: "registry.example/frontend:v1", Port: 8080, Gateway: GatewayParent{Name: "public"}}

	frontend.Spec.Replicas = nil
	deployment, _, _, err := frontendDesiredResources(frontend, profile)
	if err != nil {
		t.Fatal(err)
	}
	if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas != 1 {
		t.Fatalf("default frontend replicas = %#v, want 1", deployment.Spec.Replicas)
	}

	frontend.Spec.Replicas = ptr(int32(0))
	deployment, _, _, err = frontendDesiredResources(frontend, profile)
	if err != nil {
		t.Fatal(err)
	}
	if deployment.Spec.Replicas == nil || *deployment.Spec.Replicas != 0 {
		t.Fatalf("explicit frontend replicas = %#v, want 0", deployment.Spec.Replicas)
	}
}

func TestFrontendServiceProjectsSelectedRouterPipeline(t *testing.T) {
	frontend := testFrontendService("chat")
	frontend.Spec.RouterPipeline = inferencev1alpha1.RouterPipeline{
		Filter: inferencev1alpha1.RouterFilterAllowAll,
		Scorer: inferencev1alpha1.RouterScorerUniform,
		Picker: inferencev1alpha1.RouterPickerMax,
	}
	deployment, _, _, err := frontendDesiredResources(frontend, FrontendRuntimeProfile{Image: "registry.example/frontend:v1", Port: 8080, Gateway: GatewayParent{Name: "public"}})
	if err != nil {
		t.Fatal(err)
	}
	env := map[string]string{}
	for _, variable := range deployment.Spec.Template.Spec.Containers[0].Env {
		env[variable.Name] = variable.Value
	}
	if env["FORETOKEN_ROUTER_FILTER"] != "allow_all" || env["FORETOKEN_ROUTER_SCORER"] != "uniform" || env["FORETOKEN_ROUTER_PICKER"] != "max" {
		t.Fatalf("router pipeline stage env = %#v", env)
	}
}

func TestFrontendServicePublishesVersionedServingSnapshot(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	firstService, firstPool, first := testRoutableModelGroup("model-a", "group-a")
	secondService, secondPool, second := testRoutableModelGroup("model-b", "group-b")
	second.Spec.Artifacts.Model = "Qwen/Qwen3-1.7B"
	second.Spec.Artifacts.Tokenizer = "Qwen/Qwen3-1.7B"
	first.Spec.MaxInputTokens = ptr(int32(16384))
	second.Spec.MaxInputTokens = ptr(int32(8192))
	setModelGroupReady(first)
	setModelGroupReady(second)
	reconciler, kubeClient := testFrontendReconciler(t, frontend, firstService, firstPool, first, secondService, secondPool, second)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(frontend)}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}

	configMap := new(corev1.ConfigMap)
	key := client.ObjectKey{Namespace: frontend.Namespace, Name: frontend.Name + "-serving"}
	if err := kubeClient.Get(ctx, key, configMap); err != nil {
		t.Fatal(err)
	}
	var snapshot servingSnapshot
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if snapshot.Version != 1 || len(snapshot.Groups) != 2 || snapshot.Groups[0].RouteTargetID != string(first.UID) || snapshot.Groups[1].RouteTargetID != string(second.UID) || snapshot.Groups[0].Endpoint != "http://group-a.default.svc:9000" || snapshot.Groups[1].Endpoint != "http://group-b.default.svc:9000" || !slices.Equal(snapshot.Groups[0].Capabilities, []string{"chat", "text"}) || !slices.Equal(snapshot.Groups[1].Capabilities, []string{"chat", "text"}) || snapshot.Groups[0].MaxInputTokens == nil || *snapshot.Groups[0].MaxInputTokens != 16384 || snapshot.Groups[1].MaxInputTokens == nil || *snapshot.Groups[1].MaxInputTokens != 8192 {
		t.Fatalf("routing snapshot = %#v", snapshot)
	}

	first.Spec.Runtime.Port = 9001
	if err := kubeClient.Update(ctx, first); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, key, configMap); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if snapshot.Version != 2 || snapshot.Groups[0].Endpoint != "http://group-a.default.svc:9001" {
		t.Fatalf("updated routing snapshot = %#v", snapshot)
	}

	first.Spec.MaxInputTokens = ptr(int32(12288))
	if err := kubeClient.Update(ctx, first); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, key, configMap); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if snapshot.Version != 3 || snapshot.Groups[0].MaxInputTokens == nil || *snapshot.Groups[0].MaxInputTokens != 12288 {
		t.Fatalf("maxInputTokens routing update = %#v", snapshot)
	}

	if err := kubeClient.Delete(ctx, configMap); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, key, configMap); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if snapshot.Version != 4 {
		t.Fatalf("recreated snapshot version = %d, want 4", snapshot.Version)
	}
}

func TestServingSnapshotProjectsTypedFeatures(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	service, pool, group := testRoutableModelGroup("model", "group")
	group.Spec.Features = inferencev1alpha1.ModelFeatures{Tools: true, Reasoning: true, StructuredOutputs: []inferencev1alpha1.StructuredOutputFormat{inferencev1alpha1.StructuredOutputFormatJSONSchema, inferencev1alpha1.StructuredOutputFormatJSONObject}, Multimodal: []inferencev1alpha1.MultimodalModality{inferencev1alpha1.MultimodalModalityImage}}
	setModelGroupReady(group)
	reconciler, kubeClient := testFrontendReconciler(t, frontend, service, pool, group)
	if installed, err := reconciler.reconcileServingSnapshot(ctx, frontend); err != nil || !installed {
		t.Fatalf("reconcileServingSnapshot() = %v, %v", installed, err)
	}
	configMap := new(corev1.ConfigMap)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: frontend.Name + "-serving"}, configMap); err != nil {
		t.Fatal(err)
	}
	var snapshot servingSnapshot
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	want := []string{"chat", "text", "multimodal", "multimodal.image", "reasoning", "structured_output.json_object", "structured_output.json_schema", "tool_calling"}
	if len(snapshot.Groups) != 1 || !slices.Equal(snapshot.Groups[0].Capabilities, want) {
		t.Fatalf("routing capabilities = %#v, want %#v", snapshot.Groups, want)
	}
}

func TestRoutingCapabilitiesDefaultToChatAndText(t *testing.T) {
	if got := routingCapabilities(inferencev1alpha1.ModelFeatures{}); !slices.Equal(got, []string{"chat", "text"}) {
		t.Fatalf("default capabilities = %#v", got)
	}
}

func TestServingSnapshotEqualityIncludesMaxInputTokens(t *testing.T) {
	limit := int32(16384)
	group := servingSnapshotGroup{RouteTargetID: "group", MaxInputTokens: &limit}
	changedGroup := group
	changedGroup.MaxInputTokens = ptr(int32(8192))
	if equalRoutingGroup(group, changedGroup) {
		t.Fatal("aggregate routing equality ignored maxInputTokens")
	}

	component := servingSnapshotPDComponent{RouteTargetID: "component", MaxInputTokens: &limit}
	changedComponent := component
	changedComponent.MaxInputTokens = ptr(int32(8192))
	if equalRoutingPDComponent(component, changedComponent) {
		t.Fatal("P/D routing equality ignored maxInputTokens")
	}
}

func TestFrontendRoutingUsesOnlyThePoolActiveRevision(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	service, pool, active := testRoutableModelGroup("model", "active-group")
	setModelGroupReady(active)
	old := active.DeepCopy()
	old.Name = "old-group"
	old.UID = "old-group-uid"
	old.Spec.Revision = "old-revision"
	setModelGroupReady(old)
	reconciler, _ := testFrontendReconciler(t, frontend, service, pool, active, old)
	groups, err := reconciler.projectableGroups(ctx, frontend.Namespace)
	if err != nil {
		t.Fatal(err)
	}
	if len(groups) != 1 || groups[0].RouteTargetID != string(active.UID) {
		t.Fatalf("projected groups = %#v, want only active revision", groups)
	}
}

func TestFrontendServicePublishesPDLinksWithoutAggregateGroups(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	service, prefillPool, prefill, decodePool, decode := testRoutablePDGroups()
	reconciler, kubeClient := testFrontendReconciler(t, frontend, service, prefillPool, prefill, decodePool, decode)

	if installed, err := reconciler.reconcileServingSnapshot(ctx, frontend); err != nil || !installed {
		t.Fatalf("reconcileServingSnapshot() = %v, %v", installed, err)
	}
	configMap := new(corev1.ConfigMap)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: frontend.Name + "-serving"}, configMap); err != nil {
		t.Fatal(err)
	}
	var snapshot servingSnapshot
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if snapshot.Version != 1 || len(snapshot.Groups) != 0 || len(snapshot.PDComponents) != 2 || len(snapshot.PDDomains) != 1 {
		t.Fatalf("routing snapshot = %#v", snapshot)
	}
	if snapshot.PDDomains[0].DomainID != "pd:pd-model-uid" || len(snapshot.PDDomains[0].PrefillRouteTargetIDs) != 1 || len(snapshot.PDDomains[0].DecodeRouteTargetIDs) != 1 {
		t.Fatalf("P/D domain = %#v", snapshot.PDDomains[0])
	}
	component := snapshot.PDComponents[0]
	if component.RouteTargetID != "decode-group-uid" && component.RouteTargetID != "prefill-group-uid" {
		t.Fatalf("P/D components = %#v", snapshot.PDComponents)
	}
	if component.MaxInputTokens == nil || *component.MaxInputTokens != 16384 {
		t.Fatalf("P/D component maxInputTokens = %#v", component)
	}
}

func TestFrontendServicePublishesAtomicEPDTriplets(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	service, prefillPool, prefill, decodePool, decode := testRoutablePDGroups()
	service.Spec.ModelPools = []inferencev1alpha1.ModelPoolTemplate{{Name: "encoder", Role: inferencev1alpha1.ModelRoleEncoder}, {Name: "prefill", Role: inferencev1alpha1.ModelRolePrefill}, {Name: "decode", Role: inferencev1alpha1.ModelRoleDecode}}
	service.Spec.ECProfile = &inferencev1alpha1.ECProfileReference{Profile: "verified-ec"}
	encoderPool := prefillPool.DeepCopy()
	encoderPool.Name, encoderPool.UID = "pd-model-encoder-pool", "pd-model-encoder-pool-uid"
	encoderPool.Spec.PoolName, encoderPool.Spec.Template.Role = "encoder", inferencev1alpha1.ModelRoleEncoder
	encoderPool.OwnerReferences[0].Name, encoderPool.OwnerReferences[0].UID = service.Name, service.UID
	encoder := prefill.DeepCopy()
	encoder.Name, encoder.UID = "encoder-group", "encoder-group-uid"
	encoder.Spec.ModelPoolRef = inferencev1alpha1.LocalObjectReference{Name: encoderPool.Name, UID: string(encoderPool.UID)}
	encoder.Spec.Role, encoder.Spec.PDRuntime = inferencev1alpha1.ModelRoleEncoder, nil
	encoder.Spec.ECRuntime = &inferencev1alpha1.ModelGroupECRuntimeConfig{ProfileName: "verified-ec", ProfileRevision: "r2", Connector: "ECExampleConnector", Role: inferencev1alpha1.ECTransferRoleProducer, RuntimeFingerprint: "pinned", SharedStorageClaim: "ec-rwx", SharedStoragePath: "/var/lib/foretoken/ec"}
	encoder.OwnerReferences[0].Name, encoder.OwnerReferences[0].UID = encoderPool.Name, encoderPool.UID
	prefill.Spec.ECRuntime = &inferencev1alpha1.ModelGroupECRuntimeConfig{ProfileName: "verified-ec", ProfileRevision: "r2", Connector: "ECExampleConnector", Role: inferencev1alpha1.ECTransferRoleConsumer, RuntimeFingerprint: "pinned", SharedStorageClaim: "ec-rwx", SharedStoragePath: "/var/lib/foretoken/ec"}

	reconciler, kubeClient := testFrontendReconciler(t, frontend, service, encoderPool, encoder, prefillPool, prefill, decodePool, decode)
	if installed, err := reconciler.reconcileServingSnapshot(ctx, frontend); err != nil || !installed {
		t.Fatalf("reconcileServingSnapshot() = %v, %v", installed, err)
	}
	configMap := new(corev1.ConfigMap)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: frontend.Name + "-serving"}, configMap); err != nil {
		t.Fatal(err)
	}
	var snapshot servingSnapshot
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if len(snapshot.Groups) != 0 || len(snapshot.PDComponents) != 0 || len(snapshot.EPDComponents) != 3 || len(snapshot.EPDDomains) != 1 {
		t.Fatalf("routing snapshot = %#v", snapshot)
	}
	domain := snapshot.EPDDomains[0]
	if domain.EncoderRouteTargetID != "encoder-group-uid" || domain.PrefillRouteTargetID != "prefill-group-uid" || domain.DecodeRouteTargetID != "decode-group-uid" {
		t.Fatalf("EPD domain = %#v", domain)
	}
}

func TestFrontendServicePDProjectionFailsClosedForMissingPeerAndIncompatibleProfile(t *testing.T) {
	ctx := context.Background()
	for _, scenario := range []struct {
		name       string
		withDecode bool
		mutate     func(*inferencev1alpha1.ModelGroup)
	}{
		{name: "missing decode"},
		{name: "incompatible profile", withDecode: true, mutate: func(group *inferencev1alpha1.ModelGroup) { group.Spec.PDRuntime.ProfileRevision = "different-profile" }},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			frontend := testFrontendService("chat")
			service, prefillPool, prefill, decodePool, decode := testRoutablePDGroups()
			objects := []client.Object{frontend, service, prefillPool, prefill}
			if scenario.withDecode {
				scenario.mutate(decode)
				objects = append(objects, decodePool, decode)
			}
			reconciler, kubeClient := testFrontendReconciler(t, objects...)
			installed, err := reconciler.reconcileServingSnapshot(ctx, frontend)
			if err == nil || installed {
				t.Fatalf("reconcileServingSnapshot() = %v, %v; want fail-closed P/D error", installed, err)
			}
			var projectionErr *pdRoutingProjectionError
			if !errors.As(err, &projectionErr) {
				t.Fatalf("reconcileServingSnapshot() error = %T, want *pdRoutingProjectionError", err)
			}
			var snapshot servingSnapshot
			configMap := new(corev1.ConfigMap)
			if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: frontend.Name + "-serving"}, configMap); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
				t.Fatal(err)
			}
			if len(snapshot.Groups) != 0 || len(snapshot.PDComponents) != 0 {
				t.Fatalf("fail-closed routing snapshot = %#v", snapshot)
			}
		})
	}
}

func TestFrontendServiceRoutingProjectionPreservesAggregateRoutesWhenPDServiceIsIncomplete(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	aggregateService, aggregatePool, aggregate := testRoutableModelGroup("aggregate-model", "aggregate-group")
	setModelGroupReady(aggregate)
	brokenService, prefillPool, prefill, _, _ := testRoutablePDGroups()
	brokenService.Name = "broken-pd-model"
	brokenService.UID = "broken-pd-model-uid"
	prefillPool.Spec.ModelServiceRef = inferencev1alpha1.LocalObjectReference{Name: brokenService.Name, UID: string(brokenService.UID)}
	prefillPool.OwnerReferences[0].Name = brokenService.Name
	prefillPool.OwnerReferences[0].UID = brokenService.UID

	reconciler, kubeClient := testFrontendReconciler(t, frontend, aggregateService, aggregatePool, aggregate, brokenService, prefillPool, prefill)
	installed, err := reconciler.reconcileServingSnapshot(ctx, frontend)
	if err != nil || !installed {
		t.Fatalf("reconcileServingSnapshot() = %v, %v; want aggregate route preserved", installed, err)
	}
	configMap := new(corev1.ConfigMap)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: frontend.Name + "-serving"}, configMap); err != nil {
		t.Fatal(err)
	}
	var snapshot servingSnapshot
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if len(snapshot.Groups) != 1 || snapshot.Groups[0].RouteTargetID != string(aggregate.UID) || len(snapshot.PDComponents) != 0 {
		t.Fatalf("isolated routing snapshot = %#v", snapshot)
	}
}

func TestFrontendServiceRoutingProjectionRejectsIncompatibleGroups(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	firstService, firstPool, first := testRoutableModelGroup("model-a", "group-a")
	secondService, secondPool, second := testRoutableModelGroup("model-b", "group-b")
	second.Spec.Artifacts.TokenizerRevision = "different-revision"
	setModelGroupReady(first)
	setModelGroupReady(second)
	reconciler, kubeClient := testFrontendReconciler(t, frontend, firstService, firstPool, first, secondService, secondPool, second)

	installed, err := reconciler.reconcileServingSnapshot(ctx, frontend)
	if err == nil || installed {
		t.Fatalf("reconcileServingSnapshot() = %v, %v; want incompatible Group error", installed, err)
	}
	if got := err.Error(); got != "incompatible ModelGroups for public model \"Qwen/Qwen3-0.6B\": \"group-a-uid\" and \"group-b-uid\" must have identical modelRevision, tokenizer, and tokenizerRevision" {
		t.Fatalf("reconcileServingSnapshot() error = %q", got)
	}
	configMap := new(corev1.ConfigMap)
	key := client.ObjectKey{Namespace: frontend.Namespace, Name: frontend.Name + "-serving"}
	if err := kubeClient.Get(ctx, key, configMap); err != nil {
		t.Fatal(err)
	}
	var snapshot servingSnapshot
	if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
		t.Fatal(err)
	}
	if len(snapshot.Groups) != 0 {
		t.Fatalf("incompatible routing snapshot = %#v, want no Groups", snapshot)
	}
}

func TestFrontendServiceRoutingProjectionSkipsNonReadyAndInvalidOwnership(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	service, pool, group := testRoutableModelGroup("ready-model", "ready-group")
	setModelGroupReady(group)

	notReadyService, notReadyPool, notReadyGroup := testRoutableModelGroup("not-ready-model", "not-ready-group")
	setModelGroupReady(notReadyGroup)
	notReadyService.Status.Conditions[0].Status = metav1.ConditionFalse

	invalidService, invalidPool, invalidGroup := testRoutableModelGroup("invalid-model", "invalid-group")
	setModelGroupReady(invalidGroup)
	invalidGroup.OwnerReferences = nil
	invalidGroup.Spec.ModelPoolRef.UID = "wrong-pool-uid"

	reconciler, _ := testFrontendReconciler(t, frontend, service, pool, group, notReadyService, notReadyPool, notReadyGroup, invalidService, invalidPool, invalidGroup)
	groups, err := reconciler.projectableGroups(ctx, frontend.Namespace)
	if err != nil {
		t.Fatal(err)
	}
	if len(groups) != 1 || groups[0].RouteTargetID != string(group.UID) {
		t.Fatalf("projected groups = %#v, want only the valid ready ownership chain", groups)
	}
}

func TestFrontendServiceRejectsUnownedResources(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	conflict := &appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{
		Name: frontend.Name, Namespace: frontend.Namespace,
		OwnerReferences: []metav1.OwnerReference{{APIVersion: "apps/v1", Kind: "Deployment", Name: "other", UID: "other", Controller: boolPtr(true)}},
	}}
	reconciler, _ := testFrontendReconciler(t, frontend, conflict)

	if _, err := reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(frontend)}); err == nil {
		t.Fatal("unowned Deployment was adopted or overwritten")
	}
}

func TestFrontendServiceBecomesReadyAfterRuntimeAndRouteReadiness(t *testing.T) {
	ctx := context.Background()
	frontend := testFrontendService("chat")
	service, pool, group := testRoutableModelGroup("model-a", "group-a")
	setModelGroupReady(group)
	reconciler, kubeClient := testFrontendReconciler(t, frontend, service, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(frontend)}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	deployment := new(appsv1.Deployment)
	if err := kubeClient.Get(ctx, request.NamespacedName, deployment); err != nil {
		t.Fatal(err)
	}
	deployment.Status.ObservedGeneration = deployment.Generation
	deployment.Status.AvailableReplicas = *frontend.Spec.Replicas
	deployment.Status.Conditions = []appsv1.DeploymentCondition{{Type: appsv1.DeploymentAvailable, Status: corev1.ConditionTrue}}
	if err := kubeClient.Status().Update(ctx, deployment); err != nil {
		t.Fatal(err)
	}
	route := new(gatewayv1.HTTPRoute)
	if err := kubeClient.Get(ctx, request.NamespacedName, route); err != nil {
		t.Fatal(err)
	}
	gatewayNamespace := gatewayv1.Namespace("gateway-system")
	sectionName := gatewayv1.SectionName("https")
	route.Status.Parents = []gatewayv1.RouteParentStatus{{
		ParentRef: gatewayv1.ParentReference{Name: "public", Namespace: &gatewayNamespace, SectionName: &sectionName},
		Conditions: []metav1.Condition{
			{Type: string(gatewayv1.RouteConditionAccepted), Status: metav1.ConditionTrue, ObservedGeneration: route.Generation, Reason: "Accepted"},
			{Type: string(gatewayv1.RouteConditionResolvedRefs), Status: metav1.ConditionTrue, ObservedGeneration: route.Generation, Reason: "ResolvedRefs"},
		},
	}}
	if err := kubeClient.Status().Update(ctx, route); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	current := new(inferencev1alpha1.FrontendService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if routeCondition := meta.FindStatusCondition(current.Status.Conditions, frontendConditionRouteReady); routeCondition == nil || routeCondition.Status != metav1.ConditionTrue {
		t.Fatalf("RouteAccepted condition = %#v", routeCondition)
	}
	if ready := meta.FindStatusCondition(current.Status.Conditions, conditionReady); ready == nil || ready.Status != metav1.ConditionFalse || ready.Reason != "RoutingNotReady" {
		t.Fatalf("Ready without a routable Group = %#v", ready)
	}

	if err := kubeClient.Create(ctx, group); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if ready := meta.FindStatusCondition(current.Status.Conditions, conditionReady); ready == nil || ready.Status != metav1.ConditionTrue || ready.Reason != "Ready" {
		t.Fatalf("Ready condition = %#v", ready)
	}
}

func testFrontendReconciler(t *testing.T, objects ...client.Object) (*FrontendServiceReconciler, client.Client) {
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
	if err := gatewayv1.Install(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(&inferencev1alpha1.FrontendService{}, &inferencev1alpha1.ModelService{}, &inferencev1alpha1.ModelPool{}, &inferencev1alpha1.ModelGroup{}, &appsv1.Deployment{}, &gatewayv1.HTTPRoute{}).WithObjects(objects...).Build()
	return &FrontendServiceReconciler{Client: kubeClient, RuntimeProfile: FrontendRuntimeProfile{Image: "registry.example/frontend:v1", Port: 8080, Gateway: GatewayParent{Name: "public", Namespace: "gateway-system", SectionName: "https"}}}, kubeClient
}

func testFrontendService(name string) *inferencev1alpha1.FrontendService {
	return &inferencev1alpha1.FrontendService{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "FrontendService"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default", UID: types.UID(name + "-uid"), Generation: 3},
		Spec: inferencev1alpha1.FrontendServiceSpec{
			Replicas: ptr(int32(2)), Hostname: "chat.example.com",
			Resources: inferencev1alpha1.FrontendResources{Requests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}},
			Timeouts:  inferencev1alpha1.FrontendTimeouts{Request: "10m", StreamIdle: "5m"},
		},
	}
}

func setModelGroupReady(group *inferencev1alpha1.ModelGroup) {
	group.Status.ReadyMembers = group.Spec.MemberCount
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionReady, Status: metav1.ConditionTrue, Reason: "Available", Message: "Available", ObservedGeneration: group.Generation})
}

func testRoutableModelGroup(serviceName, groupName string) (*inferencev1alpha1.ModelService, *inferencev1alpha1.ModelPool, *inferencev1alpha1.ModelGroup) {
	service := &inferencev1alpha1.ModelService{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelService"},
		ObjectMeta: metav1.ObjectMeta{Name: serviceName, Namespace: "default", UID: types.UID(serviceName + "-uid"), Generation: 1},
		Spec:       inferencev1alpha1.ModelServiceSpec{Model: "Qwen/Qwen3-0.6B", Backend: "vllm"},
		Status: inferencev1alpha1.ModelServiceStatus{
			ObservedGeneration: 1,
			Conditions:         []metav1.Condition{{Type: conditionReady, Status: metav1.ConditionTrue, Reason: "Ready", ObservedGeneration: 1}},
		},
	}
	pool, group := testModelGroup(groupName)
	pool.Name = serviceName + "-pool"
	pool.UID = types.UID(pool.Name + "-uid")
	pool.Generation = 1
	pool.Spec.ModelServiceRef = inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)}
	pool.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: service.APIVersion,
		Kind:       service.Kind,
		Name:       service.Name,
		UID:        service.UID,
		Controller: ptr(true),
	}}
	pool.Status = inferencev1alpha1.ModelPoolStatus{
		ObservedGeneration: 1,
		ActiveRevision:     group.Spec.Revision,
		Conditions:         []metav1.Condition{{Type: conditionReady, Status: metav1.ConditionTrue, Reason: "Ready", ObservedGeneration: 1}},
	}
	group.Spec.ModelPoolRef = inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)}
	group.OwnerReferences = []metav1.OwnerReference{{
		APIVersion: pool.APIVersion,
		Kind:       pool.Kind,
		Name:       pool.Name,
		UID:        pool.UID,
		Controller: ptr(true),
	}}
	return service, pool, group
}

func testRoutablePDGroups() (*inferencev1alpha1.ModelService, *inferencev1alpha1.ModelPool, *inferencev1alpha1.ModelGroup, *inferencev1alpha1.ModelPool, *inferencev1alpha1.ModelGroup) {
	service, prefillPool, prefill := testRoutableModelGroup("pd-model", "prefill-group")
	service.Spec.ModelPools = []inferencev1alpha1.ModelPoolTemplate{{Name: "prefill", Role: inferencev1alpha1.ModelRolePrefill}, {Name: "decode", Role: inferencev1alpha1.ModelRoleDecode}}
	prefillPool.Spec.Template.Role = inferencev1alpha1.ModelRolePrefill
	prefill.Spec.Role = inferencev1alpha1.ModelRolePrefill
	prefill.Spec.PDRuntime = &inferencev1alpha1.ModelGroupPDRuntimeConfig{ProfileName: "h100-rdma", ProfileRevision: "profile-r1", Connector: "MooncakeConnector", Protocol: "rdma", BootstrapPort: 29001, AbortRequestTimeoutSeconds: 30, RDMADeviceName: "mlx5_1", RDMAResourceName: "rdma/hca_shared_devices_a", RDMAResourceCount: 1}
	prefill.Spec.MaxInputTokens = ptr(int32(16384))
	setModelGroupReady(prefill)

	_, decodePool, decode := testRoutableModelGroup("unused", "decode-group")
	decodePool.Name = "pd-model-decode-pool"
	decodePool.UID = "pd-model-decode-pool-uid"
	decodePool.Spec.ModelServiceRef = inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)}
	decodePool.Spec.Template.Role = inferencev1alpha1.ModelRoleDecode
	decodePool.OwnerReferences = []metav1.OwnerReference{{APIVersion: service.APIVersion, Kind: service.Kind, Name: service.Name, UID: service.UID, Controller: ptr(true)}}
	decode.Spec.ModelPoolRef = inferencev1alpha1.LocalObjectReference{Name: decodePool.Name, UID: string(decodePool.UID)}
	decode.OwnerReferences = []metav1.OwnerReference{{APIVersion: decodePool.APIVersion, Kind: decodePool.Kind, Name: decodePool.Name, UID: decodePool.UID, Controller: ptr(true)}}
	decode.Spec.Role = inferencev1alpha1.ModelRoleDecode
	decode.Spec.PDRuntime = prefill.Spec.PDRuntime.DeepCopy()
	decode.Spec.MaxInputTokens = ptr(int32(16384))
	setModelGroupReady(decode)
	return service, prefillPool, prefill, decodePool, decode
}

func boolPtr(value bool) *bool { return &value }
