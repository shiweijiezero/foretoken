// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package tests

import (
	"context"
	"encoding/json"
	"slices"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestInvalidPDRouteWithdrawsOnlyItsService(t *testing.T) {
	ctx := context.Background()
	frontend := &inferencev1alpha1.FrontendService{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "FrontendService"},
		ObjectMeta: metav1.ObjectMeta{Name: "frontend", Namespace: "default", UID: "frontend-uid", Generation: 1},
		Spec: inferencev1alpha1.FrontendServiceSpec{
			Replicas: pointer(int32(1)), Hostname: "frontend.example.com",
			Resources: inferencev1alpha1.FrontendResources{Requests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}},
			Timeouts:  inferencev1alpha1.FrontendTimeouts{Request: "10m", StreamIdle: "5m"},
		},
	}
	aggregate := modelService("aggregate", 1)
	aggregatePool := modelPool(aggregate, "aggregate-pool", 1)
	aggregatePool.Spec.Template.Features.Tools = true
	aggregateGroup := modelGroup(aggregatePool, "aggregate-r1-0", 0)
	aggregateGroup.Spec.Features.Tools = true
	markPoolRoutingReady(aggregatePool, "r1")
	markServiceRoutingReady(aggregate, aggregatePool)
	markGroupReady(aggregateGroup)

	pd := modelService("pd", 1)
	pd.Spec.Model = "org/pd-model"
	prefillPool := modelPool(pd, "pd-prefill", 1)
	prefillPool.Spec.PoolName = "prefill"
	prefillPool.Spec.Template.Role = inferencev1alpha1.ModelRolePrefill
	decodePool := modelPool(pd, "pd-decode", 1)
	decodePool.Spec.PoolName = "decode"
	decodePool.Spec.Template.Role = inferencev1alpha1.ModelRoleDecode
	prefill := modelGroup(prefillPool, "pd-prefill-r1-0", 0)
	decode := modelGroup(decodePool, "pd-decode-r1-0", 0)
	prefill.Spec.Role = inferencev1alpha1.ModelRolePrefill
	decode.Spec.Role = inferencev1alpha1.ModelRoleDecode
	prefill.Spec.PDRuntime = pdRuntime()
	decode.Spec.PDRuntime = pdRuntime()
	markPoolRoutingReady(prefillPool, "r1")
	markPoolRoutingReady(decodePool, "r1")
	markServiceRoutingReady(pd, prefillPool, decodePool)
	markGroupReady(prefill)
	markGroupReady(decode)

	c := controllerClient(t, frontend, aggregate, aggregatePool, aggregateGroup, pd, prefillPool, decodePool, prefill, decode)
	r := &controllers.FrontendServiceReconciler{Client: c, RuntimeProfile: controllers.FrontendRuntimeProfile{Image: "frontend:test", Port: 8080, Gateway: &controllers.GatewayParent{Name: "gateway", Namespace: "gateway-system"}}}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(frontend)}
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	assertRoutingCounts(t, ctx, c, frontend.Namespace, 1, 2)
	assertAdmissionSetSizes(t, ctx, c, frontend.Namespace, map[string][]int{aggregate.Spec.Model: {1}, pd.Spec.Model: {2}})
	assertSnapshotModelCapability(t, ctx, c, frontend.Namespace, aggregate.Spec.Model, "tool_calling")

	// A new split-role spec must not reinterpret the previously committed aggregate cohort.
	currentAggregate := get(t, ctx, c, client.ObjectKeyFromObject(aggregate), new(inferencev1alpha1.ModelService))
	currentAggregate.Spec.ModelPools = []inferencev1alpha1.ModelPoolTemplate{
		{Name: "prefill", Role: inferencev1alpha1.ModelRolePrefill, Resources: aggregatePool.Spec.Template.Resources, Parallelism: inferencev1alpha1.Parallelism{}},
		{Name: "decode", Role: inferencev1alpha1.ModelRoleDecode, Resources: aggregatePool.Spec.Template.Resources, Parallelism: inferencev1alpha1.Parallelism{}},
	}
	currentAggregate.Spec.Resources = nil
	currentAggregate.Spec.Parallelism = nil
	currentAggregate.Generation++
	if err := c.Update(ctx, currentAggregate); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	assertRoutingCounts(t, ctx, c, frontend.Namespace, 1, 2)

	currentDecode := get(t, ctx, c, client.ObjectKeyFromObject(decode), new(inferencev1alpha1.ModelGroup))
	currentDecode.Spec.Artifacts.ModelRevision = "incompatible"
	if err := c.Update(ctx, currentDecode); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	assertRoutingCounts(t, ctx, c, frontend.Namespace, 1, 0)
}

func markServiceRoutingReady(service *inferencev1alpha1.ModelService, pools ...*inferencev1alpha1.ModelPool) {
	service.Status.ObservedGeneration = service.Generation
	service.Status.ServingGeneration = service.Generation
	for _, pool := range pools {
		service.Status.ServingPoolRevisions = append(service.Status.ServingPoolRevisions, inferencev1alpha1.ServingPoolRevision{PoolName: pool.Spec.PoolName, PoolUID: string(pool.UID), Revision: pool.Status.PreparedRevision})
	}
	for _, conditionType := range []string{"IntentCompiled", "PoolsMaterialized", readyCondition} {
		meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionType, Status: metav1.ConditionTrue, Reason: "Ready", ObservedGeneration: service.Generation})
	}
}

func markPoolRoutingReady(pool *inferencev1alpha1.ModelPool, revision string) {
	pool.Status.ObservedGeneration = pool.Generation
	pool.Status.PreparedRevision = revision
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: readyCondition, Status: metav1.ConditionTrue, Reason: "Ready", ObservedGeneration: pool.Generation})
}

func pdRuntime() *inferencev1alpha1.ModelGroupPDRuntimeConfig {
	return &inferencev1alpha1.ModelGroupPDRuntimeConfig{
		ProfileName: "pd", ProfileRevision: "r1", Connector: "MooncakeConnector", Protocol: "rdma",
		BootstrapPort: 29001, AbortRequestTimeoutSeconds: 30, RDMADeviceName: "mlx5_1", RDMAResourceName: "rdma/ib", RDMAResourceCount: 1,
	}
}

func assertRoutingCounts(t *testing.T, ctx context.Context, c client.Client, namespace string, groups, pdComponents int) {
	t.Helper()
	payload := servingSnapshotPayload(t, ctx, c, namespace)
	var decoded struct {
		Groups       []json.RawMessage `json:"groups"`
		PDComponents []json.RawMessage `json:"pd_components"`
	}
	if err := json.Unmarshal([]byte(payload), &decoded); err != nil {
		t.Fatal(err)
	}
	if len(decoded.Groups) != groups || len(decoded.PDComponents) != pdComponents {
		t.Fatalf("routing counts = groups:%d pd:%d; snapshot = %s", len(decoded.Groups), len(decoded.PDComponents), payload)
	}
}

func assertAdmissionSetSizes(t *testing.T, ctx context.Context, c client.Client, namespace string, expected map[string][]int) {
	t.Helper()
	var decoded struct {
		Models []struct {
			Model               string       `json:"model"`
			AdmissionTargetSets [][]struct{} `json:"admission_target_sets"`
		} `json:"models"`
	}
	if err := json.Unmarshal([]byte(servingSnapshotPayload(t, ctx, c, namespace)), &decoded); err != nil {
		t.Fatal(err)
	}
	for _, model := range decoded.Models {
		want, exists := expected[model.Model]
		if !exists {
			continue
		}
		got := make([]int, len(model.AdmissionTargetSets))
		for index := range model.AdmissionTargetSets {
			got[index] = len(model.AdmissionTargetSets[index])
		}
		if !slices.Equal(got, want) {
			t.Fatalf("admission target set sizes for %q = %v, want %v", model.Model, got, want)
		}
		delete(expected, model.Model)
	}
	if len(expected) != 0 {
		t.Fatalf("models missing admission target sets: %v", expected)
	}
}

func assertSnapshotModelCapability(t *testing.T, ctx context.Context, c client.Client, namespace, model, capability string) {
	t.Helper()
	var decoded struct {
		Models []struct {
			Model        string   `json:"model"`
			Capabilities []string `json:"capabilities"`
		} `json:"models"`
	}
	if err := json.Unmarshal([]byte(servingSnapshotPayload(t, ctx, c, namespace)), &decoded); err != nil {
		t.Fatal(err)
	}
	for _, entry := range decoded.Models {
		if entry.Model == model && slices.Contains(entry.Capabilities, capability) {
			return
		}
	}
	t.Fatalf("model %q does not advertise capability %q: %#v", model, capability, decoded.Models)
}

func servingSnapshotPayload(t *testing.T, ctx context.Context, c client.Client, namespace string) string {
	t.Helper()
	var snapshots corev1.ConfigMapList
	if err := c.List(ctx, &snapshots, client.InNamespace(namespace)); err != nil {
		t.Fatal(err)
	}
	for _, snapshot := range snapshots.Items {
		if payload := snapshot.Data["serving.json"]; payload != "" {
			return payload
		}
	}
	t.Fatal("serving snapshot was not published")
	return ""
}
