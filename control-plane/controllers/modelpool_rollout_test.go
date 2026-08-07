// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Tests ModelPool revision rollout, scale-down, and apply failure behavior.

package controllers

import (
	"context"
	"errors"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type failingModelGroupCreateClient struct {
	client.Client
	err error
}

func (testClient failingModelGroupCreateClient) Create(ctx context.Context, object client.Object, options ...client.CreateOption) error {
	group, ok := object.(*inferencev1alpha1.ModelGroup)
	if ok && group.Spec.Ordinal == 1 {
		return testClient.err
	}
	return testClient.Client.Create(ctx, object, options...)
}

func TestModelPoolConfigUpdateActivatesNewRevisionBeforeDeletingOld(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool("demo", 1)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	reconcilePoolTwice(t, ctx, reconciler, request)

	groups := listModelGroups(t, ctx, kubeClient, pool.Namespace)
	oldGroup := &groups[0]
	setModelGroupReady(oldGroup)
	if err := kubeClient.Status().Update(ctx, oldGroup); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	oldName, oldRevision := oldGroup.Name, oldGroup.Spec.Revision
	current := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ActiveRevision != oldRevision {
		t.Fatalf("initial active revision = %q, want %q", current.Status.ActiveRevision, oldRevision)
	}

	current.Spec.Template.ModelRevision = "next-revision"
	current.Generation++
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	groups = listModelGroups(t, ctx, kubeClient, pool.Namespace)
	if len(groups) != 2 {
		t.Fatalf("Groups during rollout = %#v, want old and target revisions", groups)
	}
	var target *inferencev1alpha1.ModelGroup
	for index := range groups {
		if groups[index].Spec.Revision != oldRevision {
			target = &groups[index]
		}
	}
	if target == nil || target.Spec.Artifacts.ModelRevision != "next-revision" {
		t.Fatalf("target ModelGroup = %#v", target)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ActiveRevision != oldRevision {
		t.Fatalf("active revision changed before target readiness: %q", current.Status.ActiveRevision)
	}

	meta.SetStatusCondition(&target.Status.Conditions, metav1.Condition{
		Type:               conditionSchedulingCapacity,
		Status:             metav1.ConditionFalse,
		Reason:             "InsufficientCapacity",
		Message:            "Unschedulable",
		ObservedGeneration: target.Generation,
	})
	if err := kubeClient.Status().Update(ctx, target); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ActiveRevision != oldRevision {
		t.Fatalf("active revision during capacity-constrained rollout = %q, want %q", current.Status.ActiveRevision, oldRevision)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionRolloutPending); condition == nil || condition.Status != metav1.ConditionTrue || condition.Reason != "InsufficientCapacity" {
		t.Fatalf("RolloutPending condition = %#v", condition)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionTrue || condition.Reason != "ActiveRevisionReady" {
		t.Fatalf("Ready condition while target is pending = %#v", condition)
	}
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: pool.Namespace, Name: oldName}, new(inferencev1alpha1.ModelGroup)); err != nil {
		t.Fatalf("old ModelGroup was deleted during capacity-constrained rollout: %v", err)
	}

	setModelGroupReady(target)
	if err := kubeClient.Status().Update(ctx, target); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ActiveRevision != target.Spec.Revision {
		t.Fatalf("active revision after target readiness = %q, want %q", current.Status.ActiveRevision, target.Spec.Revision)
	}
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: pool.Namespace, Name: oldName}, new(inferencev1alpha1.ModelGroup)); err != nil {
		t.Fatalf("old ModelGroup was deleted before active revision publication: %v", err)
	}

	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: pool.Namespace, Name: oldName}, new(inferencev1alpha1.ModelGroup)); !apierrors.IsNotFound(err) {
		t.Fatalf("superseded ModelGroup %q still exists after cutover: %v", oldName, err)
	}
}

func TestModelPoolScaleDownKeepsReadyWhileSurplusGroupDrains(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool("demo", 2)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	reconcilePoolTwice(t, ctx, reconciler, request)

	groups := listModelGroups(t, ctx, kubeClient, pool.Namespace)
	for index := range groups {
		setModelGroupReady(&groups[index])
		if err := kubeClient.Status().Update(ctx, &groups[index]); err != nil {
			t.Fatal(err)
		}
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	current := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	activeRevision := current.Status.ActiveRevision

	groups = listModelGroups(t, ctx, kubeClient, pool.Namespace)
	for index := range groups {
		if groups[index].Spec.Ordinal != 1 {
			continue
		}
		groups[index].Finalizers = []string{modelGroupDrainFinalizer}
		if err := kubeClient.Update(ctx, &groups[index]); err != nil {
			t.Fatal(err)
		}
	}
	current.Spec.DesiredGroups = 1
	current.Generation++
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}

	groups = listModelGroups(t, ctx, kubeClient, pool.Namespace)
	if len(groups) != 2 {
		t.Fatalf("Groups while surplus drains = %#v", groups)
	}
	var surplus *inferencev1alpha1.ModelGroup
	for index := range groups {
		if groups[index].Spec.Ordinal == 1 {
			surplus = &groups[index]
		}
	}
	if surplus == nil || surplus.DeletionTimestamp.IsZero() {
		t.Fatalf("surplus ModelGroup = %#v", surplus)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ActiveRevision != activeRevision {
		t.Fatalf("active revision = %q, want %q", current.Status.ActiveRevision, activeRevision)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("Ready condition during scale-down = %#v", condition)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionRolloutPending); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("RolloutPending condition during scale-down = %#v", condition)
	}
}

func TestModelPoolApplyFailureDoesNotReportIncompleteActiveRevisionReady(t *testing.T) {
	ctx := context.Background()
	pool := testModelPool("demo", 1)
	reconciler, kubeClient := testPoolReconciler(t, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	reconcilePoolTwice(t, ctx, reconciler, request)

	groups := listModelGroups(t, ctx, kubeClient, pool.Namespace)
	setModelGroupReady(&groups[0])
	if err := kubeClient.Status().Update(ctx, &groups[0]); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	current := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	activeRevision := current.Status.ActiveRevision
	current.Spec.DesiredGroups = 2
	current.Generation++
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}

	createErr := errors.New("injected ModelGroup create failure")
	reconciler.Client = failingModelGroupCreateClient{Client: kubeClient, err: createErr}
	if _, err := reconciler.Reconcile(ctx, request); !errors.Is(err, createErr) {
		t.Fatalf("Reconcile error = %v, want %v", err, createErr)
	}
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ActiveRevision != activeRevision {
		t.Fatalf("active revision = %q, want %q", current.Status.ActiveRevision, activeRevision)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionGroupsMaterialized); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("GroupsMaterialized condition = %#v", condition)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "GroupsNotReady" {
		t.Fatalf("Ready condition after apply failure = %#v", condition)
	}
}
