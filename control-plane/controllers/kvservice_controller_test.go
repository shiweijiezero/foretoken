// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"encoding/json"
	"strings"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/yaml"
)

func TestKVServiceMaterializesMasterAndPool(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	kubeClient := testKVClient(t, service)
	reconciler := &KVServiceReconciler{Client: kubeClient}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	if result, err := reconciler.Reconcile(ctx, request); err != nil || !result.Requeue {
		t.Fatalf("first Reconcile() = (%#v, %v)", result, err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}

	masterName, configName, pvcName, _ := kvMasterNames(service)
	deployment := new(appsv1.Deployment)
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: masterName}, deployment); err != nil {
		t.Fatal(err)
	}
	if deployment.Spec.Strategy.Type != appsv1.RecreateDeploymentStrategyType || deployment.Spec.Template.Spec.Containers[0].Command[0] != "mooncake_master" {
		t.Fatalf("Master deployment = %#v", deployment.Spec)
	}
	container := deployment.Spec.Template.Spec.Containers[0]
	if container.Resources.Requests.Cpu().String() != "1" || container.Resources.Requests.Memory().String() != "1Gi" {
		t.Fatalf("Master resources = %#v", container.Resources)
	}
	config := new(corev1.ConfigMap)
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: configName}, config); err != nil {
		t.Fatal(err)
	}
	if config.Data[masterConfigKey] == "" || !metav1.IsControlledBy(config, service) {
		t.Fatalf("Master ConfigMap = %#v", config)
	}
	var masterConfig map[string]any
	if err := yaml.Unmarshal([]byte(config.Data[masterConfigKey]), &masterConfig); err != nil {
		t.Fatalf("parse master.yaml: %v", err)
	}
	if masterConfig["rpc_address"] != "0.0.0.0" || masterConfig["root_fs_dir"] != "/data/mooncake-offload" || masterConfig["cluster_id"] != "mooncake_cluster" {
		t.Fatalf("master.yaml = %#v", masterConfig)
	}
	pvc := new(corev1.PersistentVolumeClaim)
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: pvcName}, pvc); err != nil {
		t.Fatal(err)
	}
	if pvc.Spec.Resources.Requests.Storage().String() != "1073741824" || pvc.Annotations[snapshotRetentionAnnotation] != "Delete" || !metav1.IsControlledBy(pvc, service) {
		t.Fatalf("Snapshot PVC = %#v", pvc)
	}
	pool := new(inferencev1alpha1.KVPool)
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: poolObjectName(service, "rdma")}, pool); err != nil {
		t.Fatal(err)
	}
	if pool.Spec.KVServiceRef.UID != string(service.UID) || pool.Spec.DesiredGroups != 2 || pool.Spec.Template.Client.Port != 50052 || !metav1.IsControlledBy(pool, service) {
		t.Fatalf("KVPool = %#v", pool)
	}
	current := new(inferencev1alpha1.KVService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionInfrastructureReady); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("InfrastructureReady = %#v", condition)
	}
}

func TestKVPoolStaysUnreadyWithoutGroups(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	pool := &inferencev1alpha1.KVPool{ObjectMeta: metav1.ObjectMeta{Name: poolObjectName(service, "rdma"), Namespace: service.Namespace, UID: "pool"}, Spec: normalizedKVPoolSpec(service, service.Spec.StoragePools[0])}
	pool.OwnerReferences = []metav1.OwnerReference{{APIVersion: service.APIVersion, Kind: service.Kind, Name: service.Name, UID: service.UID, Controller: ptr(true)}}
	kubeClient := testKVClient(t, service, pool)
	reconciler := &KVPoolReconciler{Client: kubeClient}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	current := new(inferencev1alpha1.KVPool)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "ClientInfrastructureNotReady" {
		t.Fatalf("Ready = %#v", condition)
	}
}

func TestKVPoolNamesDoNotCollideForLongNames(t *testing.T) {
	service := testKVService()
	service.Name = strings.Repeat("a", 45)
	first, second := strings.Repeat("b", 63), strings.Repeat("b", 62)+"c"
	firstName, secondName := poolObjectName(service, first), poolObjectName(service, second)
	if firstName == secondName || len(firstName) > 63 || len(secondName) > 63 {
		t.Fatalf("long pool names collide or exceed DNS label limit: %q, %q", firstName, secondName)
	}
}

func TestSnapshotRemovalUsesRecordedRetention(t *testing.T) {
	for _, retention := range []inferencev1alpha1.RetentionPolicy{inferencev1alpha1.RetentionPolicyDelete, inferencev1alpha1.RetentionPolicyRetain} {
		t.Run(string(retention), func(t *testing.T) {
			ctx := context.Background()
			service := testKVService()
			_, _, pvcName, _ := kvMasterNames(service)
			pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: pvcName, Namespace: service.Namespace, Annotations: map[string]string{snapshotRetentionAnnotation: string(retention)}, OwnerReferences: []metav1.OwnerReference{{UID: service.UID, Controller: ptr(true)}}}}
			kubeClient := testKVClient(t, service, pvc)
			reconciler := &KVServiceReconciler{Client: kubeClient}
			if err := reconciler.reconcileSnapshotRemoval(ctx, service); err != nil {
				t.Fatal(err)
			}
			current := new(corev1.PersistentVolumeClaim)
			err := kubeClient.Get(ctx, client.ObjectKeyFromObject(pvc), current)
			if retention == inferencev1alpha1.RetentionPolicyDelete {
				if !apierrors.IsNotFound(err) {
					t.Fatalf("Delete PVC get error = %v, want not found", err)
				}
			} else if err != nil || len(current.OwnerReferences) != 0 {
				t.Fatalf("Retain PVC = %#v, err = %v", current, err)
			}
		})
	}
}

func TestRetainedPVCDoesNotBlockSameNameKVServiceRecreation(t *testing.T) {
	ctx := context.Background()
	oldService, newService := testKVService(), testKVService()
	oldService.UID, newService.UID = "old-uid", "new-uid"
	_, _, oldPVCName, _ := kvMasterNames(oldService)
	_, _, newPVCName, _ := kvMasterNames(newService)
	if oldPVCName == newPVCName {
		t.Fatalf("PVC names collide across KVService recreation: %q", oldPVCName)
	}
	retained := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: oldPVCName, Namespace: oldService.Namespace, Annotations: map[string]string{snapshotRetentionAnnotation: "Retain"}}}
	kubeClient := testKVClient(t, newService, retained)
	reconciler := &KVServiceReconciler{Client: kubeClient}
	if err := reconciler.reconcileInfrastructure(ctx, newService); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: newService.Namespace, Name: newPVCName}, new(corev1.PersistentVolumeClaim)); err != nil {
		t.Fatalf("new KVService PVC was blocked: %v", err)
	}
}

func TestKVServiceRemovesStaleRequesterConfigAfterGenerationChange(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	kubeClient := testKVClient(t, service)
	reconciler := &KVServiceReconciler{Client: kubeClient}
	if err := reconciler.reconcileInfrastructure(ctx, service); err != nil {
		t.Fatal(err)
	}
	service.Generation = 2
	if err := reconciler.reconcileInfrastructure(ctx, service); err != nil {
		t.Fatal(err)
	}

	configs := new(corev1.ConfigMapList)
	if err := kubeClient.List(ctx, configs, client.InNamespace(service.Namespace), client.MatchingLabels{"inference.foretoken.io/component": "mooncake-requester"}); err != nil {
		t.Fatal(err)
	}
	if len(configs.Items) != 1 {
		t.Fatalf("requester ConfigMaps = %d, want 1", len(configs.Items))
	}
	expected, err := desiredKVRequesterConfig(service, kvMasterNamesService(service), 50051)
	if err != nil {
		t.Fatal(err)
	}
	if configs.Items[0].Name != expected.Name {
		t.Fatalf("requester ConfigMap = %q, want %q", configs.Items[0].Name, expected.Name)
	}
}

func TestKVPoolStateRejectsStaleOrDeletingPools(t *testing.T) {
	for _, mutate := range []struct {
		name  string
		apply func(*inferencev1alpha1.KVPool)
	}{
		{"stale", func(pool *inferencev1alpha1.KVPool) { pool.Spec.DesiredGroups++ }},
		{"deleting", func(pool *inferencev1alpha1.KVPool) {
			now := metav1.Now()
			pool.DeletionTimestamp = &now
			pool.Finalizers = []string{"test"}
		}},
	} {
		t.Run(mutate.name, func(t *testing.T) {
			ctx := context.Background()
			service := testKVService()
			pool := &inferencev1alpha1.KVPool{ObjectMeta: metav1.ObjectMeta{Name: poolObjectName(service, "rdma"), Namespace: service.Namespace, OwnerReferences: []metav1.OwnerReference{{UID: service.UID, Controller: ptr(true)}}}, Spec: normalizedKVPoolSpec(service, service.Spec.StoragePools[0])}
			mutate.apply(pool)
			kubeClient := testKVClient(t, service, pool)
			materialized, _, err := (&KVServiceReconciler{Client: kubeClient}).poolState(ctx, service)
			if err != nil || materialized {
				t.Fatalf("poolState() = (%v, %v), want false, nil", materialized, err)
			}
		})
	}
}

func testKVClient(t *testing.T, objects ...client.Object) client.Client {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := inferencev1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	return fake.NewClientBuilder().WithScheme(scheme).WithStatusSubresource(&inferencev1alpha1.KVService{}, &inferencev1alpha1.KVPool{}, &inferencev1alpha1.KVGroup{}).WithObjects(objects...).Build()
}

func testKVService() *inferencev1alpha1.KVService {
	return &inferencev1alpha1.KVService{TypeMeta: metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "KVService"}, ObjectMeta: metav1.ObjectMeta{Name: "cache", Namespace: "default", UID: "cache-uid", Generation: 1}, Spec: inferencev1alpha1.KVServiceSpec{Backend: "mooncakeStandaloneStore", Master: inferencev1alpha1.KVMasterSpec{Image: "example/master:latest", Resources: inferencev1alpha1.KVResources{Requests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}}, Snapshot: &inferencev1alpha1.SnapshotStorage{Size: "1073741824"}}, StoragePools: []inferencev1alpha1.KVStoragePoolTemplate{{Name: "rdma", Replicas: 2, Client: inferencev1alpha1.KVClientTemplate{Image: "example/client:latest", Protocol: "rdma", RDMAResourceName: "rdma/ib", RDMAResourceCount: 1, Resources: inferencev1alpha1.KVResources{Requests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"}}, MemoryCapacity: "536870912", Disk: &inferencev1alpha1.KVDisk{Size: "1073741824"}}}}, Timeouts: inferencev1alpha1.KVTimeouts{Startup: "10m", Drain: "2m"}, Requester: inferencev1alpha1.KVRequesterSpec{LocalBufferSize: "4294967296"}}}
}

func TestKVServiceRequesterConfigUsesExactBytesAndUIDSafeEndpoint(t *testing.T) {
	service := testKVService()
	service.UID = "uid/with:unsafe chars"
	service.Generation = 7
	requester, err := desiredKVRequesterConfig(service, kvMasterNamesService(service), 50051)
	if err != nil {
		t.Fatal(err)
	}
	var config map[string]any
	if err := json.Unmarshal([]byte(requester.Data[requesterConfigKey]), &config); err != nil {
		t.Fatal(err)
	}
	if config["mode"] != "standalone-store" || config["metadata_server"] != "P2PHANDSHAKE" || config["local_buffer_size"] != "4294967296B" || config["global_segment_size"] != float64(0) || config["protocol"] != "rdma" || config["enable_offload"] != true {
		t.Fatalf("requester config = %#v", config)
	}
	if config["master_server_address"] != kvMasterNamesService(service)+".default.svc:50051" || requester.Name == service.Name+"-requester-config" {
		t.Fatalf("endpoint/name = %#v/%q", config["master_server_address"], requester.Name)
	}
}

func kvMasterNamesService(service *inferencev1alpha1.KVService) string {
	_, _, _, name := kvMasterNames(service)
	return name
}
