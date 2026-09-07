// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package tests

import (
	"context"
	"net"
	"net/http"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
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

// TestRuntimeCacheControllerLifecycle protects managed PVC creation, workload binding, and retention cleanup.
func TestRuntimeCacheControllerLifecycle(t *testing.T) {
	ctx := context.Background()
	cache := &inferencev1alpha1.RuntimeCache{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "RuntimeCache"},
		ObjectMeta: metav1.ObjectMeta{Name: "models", Namespace: "default", UID: "runtime-cache-uid", Generation: 1},
		Spec: inferencev1alpha1.RuntimeCacheSpec{
			InitialSize: "10Gi", AccessMode: inferencev1alpha1.RuntimeCacheAccessModeReadWriteMany,
			RetentionPolicy: inferencev1alpha1.RuntimeCacheRetentionPolicyRetain,
			MaxSize:         "100Gi",
		},
	}
	c := controllerClient(t, cache)
	r := &controllers.RuntimeCacheReconciler{Client: c}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(cache)}
	for range 2 {
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	var claims corev1.PersistentVolumeClaimList
	if err := c.List(ctx, &claims, client.InNamespace(cache.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(claims.Items) != 1 || !metav1.IsControlledBy(&claims.Items[0], cache) {
		t.Fatalf("managed runtime cache claim = %#v", claims.Items)
	}
	initialRequest := claims.Items[0].Spec.Resources.Requests[corev1.ResourceStorage]
	if initialRequest.Cmp(resource.MustParse("10Gi")) != 0 {
		t.Fatalf("managed runtime cache request = %s", initialRequest.String())
	}
	claim := &claims.Items[0]
	// WaitForFirstConsumer claims must be projected before binding so a workload can trigger provisioning.
	binding, usable, err := (controllers.RuntimeCacheProfile{MountPath: "/cache"}).Resolve(ctx, c, cache.Namespace)
	if err != nil || !usable || binding == nil || binding.ClaimName != claim.Name {
		t.Fatalf("pending runtime cache binding = %#v, usable = %v, error = %v", binding, usable, err)
	}
	claim.Status.Phase = corev1.ClaimBound
	claim.Status.Capacity = corev1.ResourceList{corev1.ResourceStorage: resource.MustParse("10Gi")}
	if err := c.Status().Update(ctx, claim); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.RuntimeCache))
	if condition := meta.FindStatusCondition(current.Status.Conditions, readyCondition); condition == nil || condition.Status != metav1.ConditionTrue || current.Status.ClaimName != claim.Name {
		t.Fatalf("ready runtime cache status = %#v", current.Status)
	}

	service := modelService("managed-cache-model", 1)
	if err := c.Create(ctx, service); err != nil {
		t.Fatal(err)
	}
	serviceReconciler := &controllers.ModelServiceReconciler{Client: c, CacheProfile: controllers.RuntimeCacheProfile{MountPath: "/cache"}}
	serviceRequest := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	for range 2 {
		if _, err := serviceReconciler.Reconcile(ctx, serviceRequest); err != nil {
			t.Fatal(err)
		}
	}
	pool := get(t, ctx, c, client.ObjectKey{Namespace: service.Namespace, Name: "managed-cache-model-default"}, new(inferencev1alpha1.ModelPool))
	if pool.Spec.Template.RuntimeCache == nil || pool.Spec.Template.RuntimeCache.ClaimName != claim.Name || pool.Spec.Template.RuntimeCache.MountPath != "/cache" || pool.Spec.Template.RuntimeCache.MinimumAvailableBytes != 10<<30 {
		t.Fatalf("managed runtime cache binding = %#v", pool.Spec.Template.RuntimeCache)
	}

	// A mounted filesystem with less free space than the initial allocation must grow before model loading proceeds.
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	observationServer := &http.Server{Handler: http.HandlerFunc(func(response http.ResponseWriter, _ *http.Request) {
		response.Header().Set("Content-Type", "application/json")
		_, _ = response.Write([]byte(`{"version":1,"pod_uid":"cache-pod-uid","capacity_bytes":10737418240,"available_bytes":5368709120}`))
	})}
	go func() { _ = observationServer.Serve(listener) }()
	t.Cleanup(func() { _ = observationServer.Shutdown(context.Background()) })
	observationPort := int32(listener.Addr().(*net.TCPAddr).Port)
	group := modelGroup(pool, "managed-cache-group", 0)
	group.Spec.Runtime.Port = observationPort - 1
	if err := c.Create(ctx, group); err != nil {
		t.Fatal(err)
	}
	pod := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "managed-cache-pod", Namespace: group.Namespace, UID: "cache-pod-uid", Labels: map[string]string{
			"inference.foretoken.io/model-group": group.Name,
			"inference.foretoken.io/model-role":  string(group.Spec.Role),
		}},
		Status: corev1.PodStatus{Phase: corev1.PodRunning, PodIP: "127.0.0.1"},
	}
	if err := c.Create(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	claim = get(t, ctx, c, client.ObjectKeyFromObject(claim), new(corev1.PersistentVolumeClaim))
	expandedRequest := claim.Spec.Resources.Requests[corev1.ResourceStorage]
	if expandedRequest.Cmp(resource.MustParse("20Gi")) != 0 {
		t.Fatalf("automatically expanded runtime cache request = %s", expandedRequest.String())
	}
	current = get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.RuntimeCache))
	if current.Status.Phase != inferencev1alpha1.RuntimeCachePhaseResizing {
		t.Fatalf("automatic expansion status = %#v", current.Status)
	}

	current = get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.RuntimeCache))
	if err := c.Delete(ctx, current); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	claim = get(t, ctx, c, client.ObjectKeyFromObject(claim), new(corev1.PersistentVolumeClaim))
	if len(claim.OwnerReferences) != 0 {
		t.Fatalf("retained runtime cache claim owners = %#v", claim.OwnerReferences)
	}
	if err := c.Get(ctx, request.NamespacedName, new(inferencev1alpha1.RuntimeCache)); !apierrors.IsNotFound(err) {
		t.Fatalf("deleted RuntimeCache lookup error = %v", err)
	}

	deleteCache := cache.DeepCopy()
	deleteCache.Name = "temporary"
	deleteCache.UID = "temporary-cache-uid"
	deleteCache.ResourceVersion = ""
	deleteCache.Generation = 1
	deleteCache.Finalizers = nil
	deleteCache.DeletionTimestamp = nil
	deleteCache.Status = inferencev1alpha1.RuntimeCacheStatus{}
	deleteCache.Spec.RetentionPolicy = inferencev1alpha1.RuntimeCacheRetentionPolicyDelete
	if err := c.Create(ctx, deleteCache); err != nil {
		t.Fatal(err)
	}
	deleteRequest := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(deleteCache)}
	for range 2 {
		if _, err := r.Reconcile(ctx, deleteRequest); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.List(ctx, &claims, client.InNamespace(deleteCache.Namespace)); err != nil {
		t.Fatal(err)
	}
	var deleteClaim *corev1.PersistentVolumeClaim
	for index := range claims.Items {
		if metav1.IsControlledBy(&claims.Items[index], deleteCache) {
			deleteClaim = &claims.Items[index]
			break
		}
	}
	if deleteClaim == nil {
		t.Fatal("delete-policy RuntimeCache did not create a PVC")
	}
	deleteCache = get(t, ctx, c, deleteRequest.NamespacedName, new(inferencev1alpha1.RuntimeCache))
	if err := c.Delete(ctx, deleteCache); err != nil {
		t.Fatal(err)
	}
	for range 2 {
		if _, err := r.Reconcile(ctx, deleteRequest); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.Get(ctx, client.ObjectKeyFromObject(deleteClaim), new(corev1.PersistentVolumeClaim)); !apierrors.IsNotFound(err) {
		t.Fatalf("delete-policy PVC lookup error = %v", err)
	}
}
