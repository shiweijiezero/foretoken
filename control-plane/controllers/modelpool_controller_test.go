// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Tests ModelPool resolution, ownership, and ModelGroup materialization.

package controllers

import (
	"context"
	"strings"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestModelPoolReconcileMaterializesGroup(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool("demo", 1)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	reconcilePoolTwice(t, ctx, reconciler, request)

	groups := listModelGroups(t, ctx, kubeClient, pool.Namespace)
	if len(groups) != 1 {
		t.Fatalf("ModelGroups = %d, want 1", len(groups))
	}
	group := groups[0]
	if !metav1.IsControlledBy(&group, pool) || group.Spec.ModelPoolRef.UID != string(pool.UID) {
		t.Fatalf("ModelGroup ownership = %#v", group)
	}
	if group.Spec.Artifacts.ModelRevision != "model-revision" || group.Spec.Resources.Requests.GPU.Type != "nvidia-h100-80gb" {
		t.Fatalf("resolved ModelGroup = %#v", group.Spec)
	}
	if group.Spec.Revision == "" || group.Spec.Ordinal != 0 {
		t.Fatalf("ModelGroup identity = %#v", group.Spec)
	}

	current := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionGroupsMaterialized); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("GroupsMaterialized condition = %#v", condition)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelPoolBecomesReadyWhenAllCurrentGroupsAreReady(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool("demo", 1)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	reconcilePoolTwice(t, ctx, reconciler, request)

	groups := listModelGroups(t, ctx, kubeClient, pool.Namespace)
	group := &groups[0]
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{
		Type:               conditionReady,
		Status:             metav1.ConditionTrue,
		Reason:             "Ready",
		Message:            "Ready",
		ObservedGeneration: group.Generation,
	})
	if err := kubeClient.Status().Update(ctx, group); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}

	current := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ActiveRevision != group.Spec.Revision {
		t.Fatalf("ModelPool active revision = %q, want %q", current.Status.ActiveRevision, group.Spec.Revision)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionTrue || condition.ObservedGeneration != current.Generation {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelPoolScaledToZeroIsNotServingReady(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool("demo", 0)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	reconcilePoolTwice(t, ctx, reconciler, request)

	current := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady)
	if condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "ScaledToZero" {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelPoolReconcileWaitsForResolution(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool("pending", 1)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	reconciler.TemplateResolver = resolver.StaticModelPoolResolver{}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	reconcilePoolTwice(t, ctx, reconciler, request)

	if groups := listModelGroups(t, ctx, kubeClient, pool.Namespace); len(groups) != 0 {
		t.Fatalf("unresolved Pool created ModelGroups: %#v", groups)
	}
	current := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionResolved); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("Resolved condition = %#v", condition)
	}
}

func TestRevisionReadyRequiresEveryDesiredOrdinal(t *testing.T) {
	readyGroup := func(ordinal int32) inferencev1alpha1.ModelGroup {
		group := inferencev1alpha1.ModelGroup{
			ObjectMeta: metav1.ObjectMeta{Generation: 1},
			Spec: inferencev1alpha1.ModelGroupSpec{
				Revision: "r1",
				Ordinal:  ordinal,
			},
		}
		setModelGroupReady(&group)
		return group
	}
	ordinal0 := readyGroup(0)
	ordinal1 := readyGroup(1)
	retiring := ordinal1.DeepCopy()
	deleting := metav1.Now()
	retiring.DeletionTimestamp = &deleting

	tests := []struct {
		name    string
		groups  []inferencev1alpha1.ModelGroup
		desired int32
		ready   bool
	}{
		{name: "missing ordinal", groups: []inferencev1alpha1.ModelGroup{ordinal0}, desired: 2},
		{name: "complete ordinals", groups: []inferencev1alpha1.ModelGroup{ordinal0, ordinal1}, desired: 2, ready: true},
		{name: "duplicate ordinal", groups: []inferencev1alpha1.ModelGroup{ordinal0, ordinal0}, desired: 2},
		{name: "retiring excess", groups: []inferencev1alpha1.ModelGroup{ordinal0, *retiring}, desired: 1, ready: true},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if ready := revisionReady(test.groups, "r1", test.desired); ready != test.ready {
				t.Fatalf("revisionReady() = %t, want %t", ready, test.ready)
			}
		})
	}
}

func TestModelPoolRejectsConflictingServiceOwnership(t *testing.T) {
	pool := testModelPool("demo", 1)
	pool.OwnerReferences = nil
	reconciler, _ := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	if _, err := reconciler.Reconcile(context.Background(), request); err == nil {
		t.Fatal("ModelPool without its ModelService owner was accepted")
	}
}

func TestModelGroupNamePreservesRevisionAndOrdinal(t *testing.T) {
	group := new(inferencev1alpha1.ModelGroup)
	setGroupName(group, strings.Repeat("a", 63), "r-example", 12)
	if len(group.Name) > 63 || !strings.HasSuffix(group.Name, "-r-example-12") {
		t.Fatalf("ModelGroup name = %q", group.Name)
	}

	dotted := new(inferencev1alpha1.ModelGroup)
	dashed := new(inferencev1alpha1.ModelGroup)
	setGroupName(dotted, "model.example-default", "r-example", 0)
	setGroupName(dashed, "model-example-default", "r-example", 0)
	if strings.Contains(dotted.Name, ".") || dotted.Name == dashed.Name {
		t.Fatalf("DNS-label-safe ModelGroup names = dotted %q, dashed %q", dotted.Name, dashed.Name)
	}
}

func TestModelPoolDoesNotAdoptConflictingGroup(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool("demo", 1)
	group := &inferencev1alpha1.ModelGroup{
		ObjectMeta: metav1.ObjectMeta{Name: "conflict", Namespace: pool.Namespace},
		Spec: inferencev1alpha1.ModelGroupSpec{
			ModelPoolRef: inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)},
		},
	}
	reconciler, _ := testPoolReconciler(t, pool, group)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	if _, err := reconciler.Reconcile(ctx, request); err == nil {
		t.Fatal("conflicting ModelGroup was adopted")
	}
}

func testPoolReconciler(t *testing.T, objects ...client.Object) (*ModelPoolReconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := inferencev1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	objects = append([]client.Object{testModelService("service", 1)}, objects...)
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&inferencev1alpha1.ModelPool{}, &inferencev1alpha1.ModelGroup{}).
		WithObjects(objects...).
		Build()
	return &ModelPoolReconciler{Client: kubeClient, TemplateResolver: resolver.StaticModelPoolResolver{RuntimeProfile: testRuntimeProfile()}}, kubeClient
}

func testModelPool(name string, desiredGroups int32) *inferencev1alpha1.ModelPool {
	return &inferencev1alpha1.ModelPool{
		TypeMeta: metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelPool"},
		ObjectMeta: metav1.ObjectMeta{
			Name:       name,
			Namespace:  "default",
			UID:        types.UID(name + "-uid"),
			Generation: 1,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: inferencev1alpha1.GroupVersion.String(),
				Kind:       "ModelService",
				Name:       "service",
				UID:        "service-uid",
				Controller: ptr(true),
			}},
		},
		Spec: inferencev1alpha1.ModelPoolSpec{
			ModelServiceRef: inferencev1alpha1.LocalObjectReference{Name: "service", UID: "service-uid"},
			PoolName:        "default",
			DesiredGroups:   desiredGroups,
			Template: inferencev1alpha1.NormalizedPoolTemplate{
				Model:         "Qwen/Qwen3-0.6B",
				ModelRevision: "model-revision",
				Backend:       "vllm",
				Role:          inferencev1alpha1.ModelRoleAggregate,
				NodeCount:     1,
				MemberCount:   1,
				Resources: inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
					ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
					GPU:                     inferencev1alpha1.GPURequest{Type: "auto", Count: 1},
				}},
				Parallelism: inferencev1alpha1.CompiledParallelism{TP: 1, PP: 1, DP: 1, PCP: 1, DCP: 1},
				Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
			},
		},
	}
}

func testRuntimeProfile() resolver.RuntimeProfile {
	return resolver.RuntimeProfile{
		Image:              "vllm:test",
		ModelServerPort:    9000,
		AcceleratorType:    "nvidia-h100-80gb",
		DeviceResourceName: "nvidia.com/gpu",
		NodeSelectorKey:    "nvidia.com/gpu.product",
		NodeSelectorValue:  "NVIDIA-H100-80GB-HBM3",
	}
}

func reconcilePoolTwice(t *testing.T, ctx context.Context, reconciler *ModelPoolReconciler, request ctrl.Request) {
	t.Helper()
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
}

func listModelGroups(t *testing.T, ctx context.Context, kubeClient client.Client, namespace string) []inferencev1alpha1.ModelGroup {
	t.Helper()
	var groups inferencev1alpha1.ModelGroupList
	if err := kubeClient.List(ctx, &groups, client.InNamespace(namespace)); err != nil {
		t.Fatal(err)
	}
	return groups.Items
}
