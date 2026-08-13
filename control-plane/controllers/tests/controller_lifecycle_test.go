// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package tests

import (
	"context"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestModelServingControllerLifecycle(t *testing.T) {
	ctx := context.Background()
	t.Run("ModelService materializes owned Pool and aggregates readiness", func(t *testing.T) {
		service := modelService("chat", 1)
		c := controllerClient(t, service)
		r := &controllers.ModelServiceReconciler{Client: c}
		request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
		for range 2 {
			if _, err := r.Reconcile(ctx, request); err != nil {
				t.Fatal(err)
			}
		}
		pool := get(t, ctx, c, client.ObjectKey{Namespace: service.Namespace, Name: "chat-default"}, new(inferencev1alpha1.ModelPool))
		if !metav1.IsControlledBy(pool, service) || pool.Spec.ModelServiceRef.UID != string(service.UID) || pool.Spec.DesiredGroups != 1 {
			t.Fatalf("materialized pool = %#v", pool)
		}
		meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: readyCondition, Status: metav1.ConditionTrue, ObservedGeneration: pool.Generation})
		pool.Status.ObservedGeneration = pool.Generation
		if err := c.Status().Update(ctx, pool); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelService))
		if condition := meta.FindStatusCondition(current.Status.Conditions, readyCondition); condition == nil || condition.Status != metav1.ConditionTrue {
			t.Fatalf("service readiness = %#v", current.Status)
		}
	})

	t.Run("ModelPool materializes groups and only cuts over a ready revision", func(t *testing.T) {
		service := modelService("rollout", 1)
		pool := modelPool(service, "rollout-default", 1)
		c := controllerClient(t, service, pool)
		r := &controllers.ModelPoolReconciler{Client: c, TemplateResolver: resolver.StaticModelPoolResolver{RuntimeProfile: resolver.RuntimeProfile{Image: "vllm:test", ModelServerPort: 9000, AcceleratorType: "nvidia-h100-80gb", DeviceResourceName: "nvidia.com/gpu", NodeSelectorKey: "nvidia.com/gpu.product", NodeSelectorValue: "NVIDIA-H100-80GB-HBM3"}}}
		request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
		for range 2 {
			if _, err := r.Reconcile(ctx, request); err != nil {
				t.Fatal(err)
			}
		}
		var groups inferencev1alpha1.ModelGroupList
		if err := c.List(ctx, &groups, client.InNamespace(pool.Namespace)); err != nil {
			t.Fatal(err)
		}
		if len(groups.Items) != 1 || !metav1.IsControlledBy(&groups.Items[0], pool) {
			t.Fatalf("initial groups = %#v", groups.Items)
		}
		markGroupReady(&groups.Items[0])
		if err := c.Status().Update(ctx, &groups.Items[0]); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelPool))
		oldRevision := current.Status.ActiveRevision
		current.Spec.Template.ModelRevision = "next"
		current.Generation++
		if err := c.Update(ctx, current); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		if err := c.List(ctx, &groups, client.InNamespace(pool.Namespace)); err != nil {
			t.Fatal(err)
		}
		if len(groups.Items) != 2 {
			t.Fatalf("rollout groups = %#v", groups.Items)
		}
		if err := c.Get(ctx, request.NamespacedName, current); err != nil {
			t.Fatal(err)
		}
		if current.Status.ActiveRevision != oldRevision {
			t.Fatalf("active revision changed before readiness: %#v", current.Status)
		}
		for i := range groups.Items {
			if groups.Items[i].Spec.Revision != oldRevision {
				markGroupReady(&groups.Items[i])
				if err := c.Status().Update(ctx, &groups.Items[i]); err != nil {
					t.Fatal(err)
				}
			}
		}
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		if err := c.Get(ctx, request.NamespacedName, current); err != nil {
			t.Fatal(err)
		}
		if current.Status.ActiveRevision == oldRevision {
			t.Fatalf("ready target did not become active: %#v", current.Status)
		}
	})

	t.Run("KVService materializes storage infrastructure, Pool, and Groups", func(t *testing.T) {
		service := kvService()
		c := controllerClient(t, service)
		serviceReconciler := &controllers.KVServiceReconciler{Client: c}
		serviceRequest := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
		for range 2 {
			if _, err := serviceReconciler.Reconcile(ctx, serviceRequest); err != nil {
				t.Fatal(err)
			}
		}
		var pools inferencev1alpha1.KVPoolList
		if err := c.List(ctx, &pools, client.InNamespace(service.Namespace)); err != nil {
			t.Fatal(err)
		}
		if len(pools.Items) != 1 || !metav1.IsControlledBy(&pools.Items[0], service) || pools.Items[0].Spec.DesiredGroups != 2 {
			t.Fatalf("KV pools = %#v", pools.Items)
		}
		poolReconciler := &controllers.KVPoolReconciler{Client: c}
		poolRequest := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(&pools.Items[0])}
		for range 2 {
			if _, err := poolReconciler.Reconcile(ctx, poolRequest); err != nil {
				t.Fatal(err)
			}
		}
		var groups inferencev1alpha1.KVGroupList
		if err := c.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
			t.Fatal(err)
		}
		if len(groups.Items) != 2 {
			t.Fatalf("KV groups = %#v", groups.Items)
		}
		for i := range groups.Items {
			if !metav1.IsControlledBy(&groups.Items[i], &pools.Items[0]) || groups.Items[i].Spec.KVPoolRef.UID != string(pools.Items[0].UID) {
				t.Fatalf("KV group ownership = %#v", groups.Items[i])
			}
		}
	})
}
