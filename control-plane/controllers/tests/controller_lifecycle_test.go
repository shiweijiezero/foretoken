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

// TestModelServingControllerLifecycle protects controller-owned pool and group materialization across serving revisions.
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
		if !metav1.IsControlledBy(pool, service) || pool.Spec.ModelServiceRef.UID != string(service.UID) || pool.Spec.DesiredGroups != 1 || pool.Spec.Template.Tokenizer != service.Spec.Model || pool.Spec.Template.ModelRevision != "main" || pool.Spec.Template.TokenizerRevision != "main" {
			t.Fatalf("materialized pool = %#v", pool)
		}
		group := modelGroup(pool, "chat-r1-0", 0)
		markGroupReady(group)
		if err := c.Create(ctx, group); err != nil {
			t.Fatal(err)
		}
		if err := c.Status().Update(ctx, group); err != nil {
			t.Fatal(err)
		}
		pool.Status.ObservedGeneration = pool.Generation
		pool.Status.PreparedRevision = group.Spec.Revision
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
		r := &controllers.ModelPoolReconciler{Client: c, TemplateResolver: resolver.StaticModelPoolResolver{RuntimeProfile: resolver.RuntimeProfile{Revision: "default", Image: "vllm:test", ModelServerPort: 9000, DeviceResourceName: "nvidia.com/gpu", NodeSelectorKey: "nvidia.com/gpu.product", NodeSelectorValue: "NVIDIA-H100-80GB-HBM3"}}}
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
		oldRevision := current.Status.PreparedRevision
		currentService := get(t, ctx, c, client.ObjectKeyFromObject(service), new(inferencev1alpha1.ModelService))
		currentService.Status.ServingGeneration = currentService.Generation
		currentService.Status.ServingPoolRevisions = []inferencev1alpha1.ServingPoolRevision{{PoolName: current.Spec.PoolName, PoolUID: string(current.UID), Revision: oldRevision}}
		if err := c.Status().Update(ctx, currentService); err != nil {
			t.Fatal(err)
		}
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
		if current.Status.PreparedRevision != oldRevision {
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
		if current.Status.PreparedRevision == oldRevision {
			t.Fatalf("ready target did not become prepared: %#v", current.Status)
		}
		if err := c.List(ctx, &groups, client.InNamespace(pool.Namespace)); err != nil {
			t.Fatal(err)
		}
		if len(groups.Items) != 2 {
			t.Fatalf("old serving cohort was retired before service commit: %#v", groups.Items)
		}
		currentService = get(t, ctx, c, client.ObjectKeyFromObject(service), new(inferencev1alpha1.ModelService))
		currentService.Status.ServingPoolRevisions[0].Revision = current.Status.PreparedRevision
		if err := c.Status().Update(ctx, currentService); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		if err := c.List(ctx, &groups, client.InNamespace(pool.Namespace)); err != nil {
			t.Fatal(err)
		}
		if len(groups.Items) != 1 || groups.Items[0].Spec.Revision != current.Status.PreparedRevision {
			t.Fatalf("old cohort was not retired after service commit: %#v", groups.Items)
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
