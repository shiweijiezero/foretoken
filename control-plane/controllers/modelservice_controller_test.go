// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Tests ModelService reconciliation, ownership, status, and deletion behavior.

package controllers

import (
	"context"
	"reflect"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

func TestModelServiceReconcileMaterializesPool(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	reconciler, kubeClient := testReconciler(t, service)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}

	result, err := reconciler.Reconcile(ctx, request)
	if err != nil || !result.Requeue {
		t.Fatalf("first Reconcile() = (%#v, %v), want requeue without error", result, err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}

	pool := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: "demo-default"}, pool); err != nil {
		t.Fatal(err)
	}
	if pool.Spec.ModelServiceRef.Name != service.Name || pool.Spec.ModelServiceRef.UID != string(service.UID) {
		t.Fatalf("ModelService reference = %#v", pool.Spec.ModelServiceRef)
	}
	if pool.Spec.PoolName != "default" || pool.Spec.DesiredGroups != 1 {
		t.Fatalf("ModelPool spec = %#v", pool.Spec)
	}
	if !metav1.IsControlledBy(pool, service) {
		t.Fatal("ModelPool is not controlled by ModelService")
	}

	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ObservedGeneration != service.Generation {
		t.Fatalf("observedGeneration = %d, want %d", current.Status.ObservedGeneration, service.Generation)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionPoolsMaterialized); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("PoolsMaterialized condition = %#v", condition)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelServiceScalePreservesTemplate(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	reconciler, kubeClient := testReconciler(t, service)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	reconcileTwice(t, ctx, reconciler, request)

	pool := new(inferencev1alpha1.ModelPool)
	poolKey := types.NamespacedName{Namespace: service.Namespace, Name: "demo-default"}
	if err := kubeClient.Get(ctx, poolKey, pool); err != nil {
		t.Fatal(err)
	}
	originalTemplate := pool.Spec.Template.DeepCopy()

	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	replicas := int32(2)
	current.Spec.Replicas = &replicas
	current.Generation = 4
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, poolKey, pool); err != nil {
		t.Fatal(err)
	}
	if pool.Spec.DesiredGroups != 2 || !reflect.DeepEqual(&pool.Spec.Template, originalTemplate) {
		t.Fatalf("scale changed Pool template: %#v", pool.Spec)
	}

	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	current.Spec.Resources.Requests.CPU = "2"
	current.Generation = 5
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, poolKey, pool); err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(&pool.Spec.Template, originalTemplate) || pool.Spec.Template.Resources.Requests.CPU != "2" {
		t.Fatalf("config change did not update Pool template: %#v", pool.Spec.Template)
	}
}

func TestModelServiceInvalidIntentDoesNotCreatePool(t *testing.T) {
	ctx := context.Background()
	service := testModelService("invalid", 2)
	service.Spec.Parallelism.TP = 2
	reconciler, kubeClient := testReconciler(t, service)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	reconcileTwice(t, ctx, reconciler, request)

	var pools inferencev1alpha1.ModelPoolList
	if err := kubeClient.List(ctx, &pools, client.InNamespace(service.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(pools.Items) != 0 {
		t.Fatalf("invalid intent created ModelPools: %#v", pools.Items)
	}
	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionIntentCompiled); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("IntentCompiled condition = %#v", condition)
	}
}

func TestModelServiceDoesNotAdoptConflictingPool(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	controllerutil.AddFinalizer(service, modelServiceFinalizer)
	conflict := &inferencev1alpha1.ModelPool{ObjectMeta: metav1.ObjectMeta{Name: "demo-default", Namespace: service.Namespace}}
	reconciler, _ := testReconciler(t, service, conflict)

	if _, err := reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}); err == nil {
		t.Fatal("conflicting ModelPool was adopted or overwritten")
	}
}

func TestModelServiceDeleteRemovesOwnedPoolsBeforeFinalizer(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	now := metav1.Now()
	service.DeletionTimestamp = &now
	controllerutil.AddFinalizer(service, modelServiceFinalizer)
	pool := &inferencev1alpha1.ModelPool{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "demo-default",
			Namespace: service.Namespace,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: service.APIVersion,
				Kind:       service.Kind,
				Name:       service.Name,
				UID:        service.UID,
				Controller: ptr(true),
			}},
		},
		Spec: inferencev1alpha1.ModelPoolSpec{
			ModelServiceRef: inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)},
			PoolName:        "default",
		},
	}
	reconciler, kubeClient := testReconciler(t, service, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}

	result, err := reconciler.Reconcile(ctx, request)
	if err != nil || !result.Requeue {
		t.Fatalf("deletion Reconcile() = (%#v, %v), want requeue", result, err)
	}
	var pools inferencev1alpha1.ModelPoolList
	if err := kubeClient.List(ctx, &pools, client.InNamespace(service.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(pools.Items) != 0 {
		t.Fatalf("ModelPools remain after deletion: %#v", pools.Items)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err == nil && controllerutil.ContainsFinalizer(current, modelServiceFinalizer) {
		t.Fatal("ModelService finalizer remains after child deletion")
	}
}

func testReconciler(t *testing.T, objects ...client.Object) (*ModelServiceReconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := inferencev1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&inferencev1alpha1.ModelService{}).
		WithObjects(objects...).
		Build()
	return &ModelServiceReconciler{Client: kubeClient}, kubeClient
}

func testModelService(name string, generation int64) *inferencev1alpha1.ModelService {
	return &inferencev1alpha1.ModelService{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelService"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default", UID: types.UID(name + "-uid"), Generation: generation},
		Spec: inferencev1alpha1.ModelServiceSpec{
			Model:   "Qwen/Qwen3-0.6B",
			Backend: "vllm",
			Resources: ptr(inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
				ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
				GPU:                     inferencev1alpha1.GPURequest{Type: "auto", Count: 1},
			}}),
			Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
			Parallelism: &inferencev1alpha1.Parallelism{TP: 1, PP: 1, PCP: 1, DCP: 1},
		},
	}
}

func reconcileTwice(t *testing.T, ctx context.Context, reconciler *ModelServiceReconciler, request ctrl.Request) {
	t.Helper()
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
}

func ptr[T any](value T) *T { return &value }
