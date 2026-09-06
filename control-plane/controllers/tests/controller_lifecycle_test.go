// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package tests

import (
	"context"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// TestModelServingControllerLifecycle protects controller-owned pool and group materialization across serving revisions.
func TestModelServingControllerLifecycle(t *testing.T) {
	ctx := context.Background()
	t.Run("model artifacts gate new Pools and feed shared serving cache", func(t *testing.T) {
		service := modelService("cached", 1)
		service.Generation = 2
		pool := modelPool(service, "cached-default", 1)
		pool.Spec.Template.ModelRevision = "old"
		service.Status.ServingGeneration = 1
		service.Status.ServingPoolRevisions = []inferencev1alpha1.ServingPoolRevision{{PoolName: "default", PoolUID: string(pool.UID), Revision: "old"}}
		meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: readyCondition, Status: metav1.ConditionTrue, Reason: "Ready", ObservedGeneration: 1})
		oldGroup := modelGroup(pool, "cached-old-0", 0)
		oldGroup.Spec.Revision = "old"
		markGroupReady(oldGroup)
		profile := controllers.ModelArtifactProfile{
			Image: "vllm:test", ClaimName: "model-cache", MountPath: "/cache/huggingface",
			Endpoint: "https://hub.example", TokenSecretName: "hf-token", TokenSecretKey: "token",
			ImagePullSecrets: []corev1.LocalObjectReference{{Name: "registry-auth"}},
		}
		c := controllerClient(t, service, pool, oldGroup)
		r := &controllers.ModelServiceReconciler{Client: c, ArtifactProfile: profile}
		request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
		for range 2 {
			if _, err := r.Reconcile(ctx, request); err != nil {
				t.Fatal(err)
			}
		}
		job := get(t, ctx, c, client.ObjectKey{Namespace: service.Namespace, Name: "model-artifacts-cacheduid"}, new(batchv1.Job))
		if !metav1.IsControlledBy(job, service) || job.Spec.Template.Spec.Volumes[0].PersistentVolumeClaim.ClaimName != "model-cache" || job.Spec.Template.Spec.Containers[0].Command[0] != "foretoken-prepare-hf-snapshot" {
			t.Fatalf("artifact Job = %#v", job)
		}
		currentPool := get(t, ctx, c, client.ObjectKeyFromObject(pool), new(inferencev1alpha1.ModelPool))
		if currentPool.Spec.Template.ModelRevision != "old" {
			t.Fatalf("serving Pool changed before artifacts were ready: %#v", currentPool.Spec.Template)
		}
		currentService := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelService))
		if condition := meta.FindStatusCondition(currentService.Status.Conditions, "ArtifactsReady"); condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "Preparing" {
			t.Fatalf("artifact status while preparing = %#v", currentService.Status)
		}
		if condition := meta.FindStatusCondition(currentService.Status.Conditions, readyCondition); condition == nil || condition.Status != metav1.ConditionTrue || condition.Reason != "ServingPreviousGeneration" {
			t.Fatalf("service readiness while preparing = %#v", currentService.Status)
		}
		oldGroup.Status.ReadyMembers = 0
		oldGroup.Status.Conditions = nil
		if err := c.Status().Update(ctx, oldGroup); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		currentService = get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelService))
		if condition := meta.FindStatusCondition(currentService.Status.Conditions, readyCondition); condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "PoolsNotReady" {
			t.Fatalf("service retained stale readiness while artifacts prepared = %#v", currentService.Status)
		}
		markGroupReady(oldGroup)
		if err := c.Status().Update(ctx, oldGroup); err != nil {
			t.Fatal(err)
		}
		job.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobComplete, Status: corev1.ConditionTrue}}
		if err := c.Status().Update(ctx, job); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		currentPool = get(t, ctx, c, client.ObjectKeyFromObject(pool), new(inferencev1alpha1.ModelPool))
		if currentPool.Spec.Template.ModelRevision != "main" || currentPool.Spec.Template.ArtifactCache == nil || currentPool.Spec.Template.ArtifactCache.ClaimName != "model-cache" || currentPool.Spec.Template.ArtifactCache.MountPath != "/cache/huggingface" {
			t.Fatalf("target Pool was not applied after artifact preparation: %#v", currentPool.Spec.Template)
		}
		currentService = get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelService))
		if condition := meta.FindStatusCondition(currentService.Status.Conditions, "ArtifactsReady"); condition == nil || condition.Status != metav1.ConditionTrue || condition.Reason != "Prepared" {
			t.Fatalf("prepared artifact status = %#v", currentService.Status)
		}

		group := modelGroup(currentPool, "cached-r1-0", 0)
		if err := c.Create(ctx, group); err != nil {
			t.Fatal(err)
		}
		groupReconciler := &controllers.ModelGroupReconciler{Client: c, ControlPlaneNamespace: "foretoken-system"}
		groupRequest := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
		for range 2 {
			if _, err := groupReconciler.Reconcile(ctx, groupRequest); err != nil {
				t.Fatal(err)
			}
		}
		deployment := get(t, ctx, c, groupRequest.NamespacedName, new(appsv1.Deployment))
		container := deployment.Spec.Template.Spec.Containers[0]
		if deployment.Spec.Template.Spec.Volumes[3].PersistentVolumeClaim.ClaimName != "model-cache" || container.VolumeMounts[3].MountPath != "/cache/huggingface" {
			t.Fatalf("model cache projection = %#v", deployment.Spec.Template.Spec)
		}

		failedService := modelService("failed-cache", 1)
		if err := c.Create(ctx, failedService); err != nil {
			t.Fatal(err)
		}
		failedRequest := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(failedService)}
		for range 2 {
			if _, err := r.Reconcile(ctx, failedRequest); err != nil {
				t.Fatal(err)
			}
		}
		failedJob := get(t, ctx, c, client.ObjectKey{Namespace: failedService.Namespace, Name: "model-artifacts-failedcacheuid"}, new(batchv1.Job))
		failedJob.Status.Conditions = []batchv1.JobCondition{{Type: batchv1.JobFailed, Status: corev1.ConditionTrue}}
		if err := c.Status().Update(ctx, failedJob); err != nil {
			t.Fatal(err)
		}
		if _, err := r.Reconcile(ctx, failedRequest); err != nil {
			t.Fatal(err)
		}
		failedService = get(t, ctx, c, failedRequest.NamespacedName, new(inferencev1alpha1.ModelService))
		if condition := meta.FindStatusCondition(failedService.Status.Conditions, "ArtifactsReady"); condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "PreparationFailed" {
			t.Fatalf("failed artifact status = %#v", failedService.Status)
		}
		if err := c.Get(ctx, client.ObjectKey{Namespace: failedService.Namespace, Name: "failed-cache-default"}, new(inferencev1alpha1.ModelPool)); !apierrors.IsNotFound(err) {
			t.Fatalf("failed artifact preparation materialized a ModelPool: %v", err)
		}
	})

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
