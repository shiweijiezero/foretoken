// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Tests ModelPool group materialization, ordinal readiness, scaling, and cutover.

package controllers

import (
	"context"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

func TestModelPoolReconcileRequiresEveryOrdinalBeforeReady(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool(2)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}

	reconcilePool(t, ctx, reconciler, request)
	groups := listPoolGroups(t, ctx, kubeClient, pool.Namespace)
	if len(groups) != 2 || groups[0].Spec.Ordinal != 0 || groups[1].Spec.Ordinal != 1 {
		t.Fatalf("materialized Groups = %#v, want ordinals 0 and 1", groups)
	}
	if groups[0].Spec.Revision == "" || groups[0].Spec.Revision != groups[1].Spec.Revision {
		t.Fatalf("group revisions = %q, %q", groups[0].Spec.Revision, groups[1].Spec.Revision)
	}

	setGroupReady(t, ctx, kubeClient, &groups[0])
	reconcilePool(t, ctx, reconciler, request)
	current := getPool(t, ctx, kubeClient, pool)
	if current.Status.ActiveRevision != "" {
		t.Fatalf("partially ready active revision = %q, want empty", current.Status.ActiveRevision)
	}
	assertPoolCondition(t, current, conditionGroupsMaterialized, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionCapacityReady, metav1.ConditionFalse)
	assertPoolCondition(t, current, conditionRolloutPending, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionPoolReady, metav1.ConditionFalse)

	groups = listPoolGroups(t, ctx, kubeClient, pool.Namespace)
	setGroupReady(t, ctx, kubeClient, &groups[1])
	reconcilePool(t, ctx, reconciler, request)
	current = getPool(t, ctx, kubeClient, pool)
	if current.Status.ActiveRevision != groups[0].Spec.Revision {
		t.Fatalf("ready active revision = %q, want %q", current.Status.ActiveRevision, groups[0].Spec.Revision)
	}
	assertPoolCondition(t, current, conditionGroupsMaterialized, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionCapacityReady, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionRolloutPending, metav1.ConditionFalse)
	assertPoolCondition(t, current, conditionPoolReady, metav1.ConditionTrue)
}

func TestModelPoolScaleUpAndDownPreservesActiveRevision(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool(1)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	makePoolReady(t, ctx, reconciler, kubeClient, pool, request)
	current := getPool(t, ctx, kubeClient, pool)
	activeRevision := current.Status.ActiveRevision

	current.Spec.DesiredGroups = 2
	current.Generation = 2
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	reconcilePool(t, ctx, reconciler, request)
	groups := listPoolGroups(t, ctx, kubeClient, pool.Namespace)
	if len(groups) != 2 || groups[1].Spec.Revision != activeRevision || groups[1].Spec.Ordinal != 1 {
		t.Fatalf("scale up Groups = %#v", groups)
	}
	current = getPool(t, ctx, kubeClient, pool)
	if current.Status.ActiveRevision != activeRevision {
		t.Fatalf("scale up active revision = %q, want %q", current.Status.ActiveRevision, activeRevision)
	}
	assertPoolCondition(t, current, conditionGroupsMaterialized, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionCapacityReady, metav1.ConditionFalse)
	assertPoolCondition(t, current, conditionRolloutPending, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionPoolReady, metav1.ConditionTrue)

	setGroupReady(t, ctx, kubeClient, &groups[1])
	reconcilePool(t, ctx, reconciler, request)
	current = getPool(t, ctx, kubeClient, pool)
	assertPoolCondition(t, current, conditionCapacityReady, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionRolloutPending, metav1.ConditionFalse)
	assertPoolCondition(t, current, conditionPoolReady, metav1.ConditionTrue)

	current.Spec.DesiredGroups = 1
	current.Generation = 3
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	reconcilePool(t, ctx, reconciler, request)
	groups = listPoolGroups(t, ctx, kubeClient, pool.Namespace)
	if len(groups) != 1 || groups[0].Spec.Ordinal != 0 || groups[0].Spec.Revision != activeRevision {
		t.Fatalf("scale down Groups = %#v", groups)
	}
	current = getPool(t, ctx, kubeClient, pool)
	if current.Status.ActiveRevision != activeRevision {
		t.Fatalf("scale down active revision = %q, want %q", current.Status.ActiveRevision, activeRevision)
	}
	assertPoolCondition(t, current, conditionCapacityReady, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionRolloutPending, metav1.ConditionFalse)
	assertPoolCondition(t, current, conditionPoolReady, metav1.ConditionTrue)
}

func TestModelPoolCutoverPublishesRevisionBeforeRetiringOldGroups(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool(1)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	makePoolReady(t, ctx, reconciler, kubeClient, pool, request)
	current := getPool(t, ctx, kubeClient, pool)
	oldRevision := current.Status.ActiveRevision

	current.Spec.Template.Network = "next-network"
	current.Generation = 2
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	reconcilePool(t, ctx, reconciler, request)
	groups := listPoolGroups(t, ctx, kubeClient, pool.Namespace)
	if len(groups) != 2 {
		t.Fatalf("Groups during target rollout = %#v", groups)
	}
	var target *inferencev1alpha1.ModelGroup
	for index := range groups {
		if groups[index].Spec.Revision != oldRevision {
			target = &groups[index]
		}
	}
	if target == nil || target.Spec.Network != "next-network" {
		t.Fatalf("target Group = %#v", target)
	}
	current = getPool(t, ctx, kubeClient, pool)
	if current.Status.ActiveRevision != oldRevision {
		t.Fatalf("active revision before target ready = %q, want %q", current.Status.ActiveRevision, oldRevision)
	}
	assertPoolCondition(t, current, conditionGroupsMaterialized, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionCapacityReady, metav1.ConditionFalse)
	assertPoolCondition(t, current, conditionRolloutPending, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionPoolReady, metav1.ConditionTrue)

	setGroupReady(t, ctx, kubeClient, target)
	reconcilePool(t, ctx, reconciler, request)
	current = getPool(t, ctx, kubeClient, pool)
	if current.Status.ActiveRevision != target.Spec.Revision {
		t.Fatalf("active revision after target readiness = %q, want %q", current.Status.ActiveRevision, target.Spec.Revision)
	}
	assertPoolCondition(t, current, conditionCapacityReady, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionRolloutPending, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionPoolReady, metav1.ConditionTrue)
	if groups = listPoolGroups(t, ctx, kubeClient, pool.Namespace); len(groups) != 2 {
		t.Fatalf("old Group was retired during publication: %#v", groups)
	}

	reconcilePool(t, ctx, reconciler, request)
	groups = listPoolGroups(t, ctx, kubeClient, pool.Namespace)
	if len(groups) != 1 || groups[0].Spec.Revision != target.Spec.Revision {
		t.Fatalf("Groups after old revision retirement = %#v", groups)
	}
	current = getPool(t, ctx, kubeClient, pool)
	assertPoolCondition(t, current, conditionCapacityReady, metav1.ConditionTrue)
	assertPoolCondition(t, current, conditionRolloutPending, metav1.ConditionFalse)
	assertPoolCondition(t, current, conditionPoolReady, metav1.ConditionTrue)
}

func testPoolReconciler(t *testing.T, objects ...client.Object) (*ModelPoolReconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := inferencev1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&inferencev1alpha1.ModelPool{}, &inferencev1alpha1.ModelGroup{}).
		WithObjects(objects...).
		Build()
	return &ModelPoolReconciler{Client: kubeClient}, kubeClient
}

func testModelPool(desired int32) *inferencev1alpha1.ModelPool {
	return &inferencev1alpha1.ModelPool{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelPool"},
		ObjectMeta: metav1.ObjectMeta{Name: "demo-default", Namespace: "default", UID: types.UID("pool-uid"), Generation: 1},
		Spec: inferencev1alpha1.ModelPoolSpec{
			ModelServiceRef: inferencev1alpha1.LocalObjectReference{Name: "demo", UID: "service-uid"},
			PoolName:        "default",
			DesiredGroups:   desired,
			Template: inferencev1alpha1.NormalizedPoolTemplate{
				Model:       "Qwen/Qwen3-0.6B",
				Backend:     "vllm",
				Role:        inferencev1alpha1.ModelRoleAggregate,
				NodeCount:   1,
				MemberCount: 1,
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

func makePoolReady(t *testing.T, ctx context.Context, reconciler *ModelPoolReconciler, kubeClient client.Client, pool *inferencev1alpha1.ModelPool, request ctrl.Request) {
	t.Helper()
	reconcilePool(t, ctx, reconciler, request)
	groups := listPoolGroups(t, ctx, kubeClient, pool.Namespace)
	for index := range groups {
		setGroupReady(t, ctx, kubeClient, &groups[index])
	}
	reconcilePool(t, ctx, reconciler, request)
}

func reconcilePool(t *testing.T, ctx context.Context, reconciler *ModelPoolReconciler, request ctrl.Request) {
	t.Helper()
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
}

func getPool(t *testing.T, ctx context.Context, kubeClient client.Client, pool *inferencev1alpha1.ModelPool) *inferencev1alpha1.ModelPool {
	t.Helper()
	current := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, client.ObjectKeyFromObject(pool), current); err != nil {
		t.Fatal(err)
	}
	return current
}

func assertPoolCondition(t *testing.T, pool *inferencev1alpha1.ModelPool, conditionType string, want metav1.ConditionStatus) {
	t.Helper()
	condition := meta.FindStatusCondition(pool.Status.Conditions, conditionType)
	if condition == nil || condition.Status != want {
		t.Fatalf("%s condition = %#v, want %s", conditionType, condition, want)
	}
}

func listPoolGroups(t *testing.T, ctx context.Context, kubeClient client.Client, namespace string) []inferencev1alpha1.ModelGroup {
	t.Helper()
	var groups inferencev1alpha1.ModelGroupList
	if err := kubeClient.List(ctx, &groups, client.InNamespace(namespace)); err != nil {
		t.Fatal(err)
	}
	return groups.Items
}

func setGroupReady(t *testing.T, ctx context.Context, kubeClient client.Client, group *inferencev1alpha1.ModelGroup) {
	t.Helper()
	current := new(inferencev1alpha1.ModelGroup)
	if err := kubeClient.Get(ctx, client.ObjectKeyFromObject(group), current); err != nil {
		t.Fatal(err)
	}
	current.Status.Phase = inferencev1alpha1.ModelGroupPhaseReady
	current.Status.ReadyMembers = current.Spec.MemberCount
	current.Status.TotalMembers = current.Spec.MemberCount
	if err := kubeClient.Status().Update(ctx, current); err != nil {
		t.Fatal(err)
	}
}
