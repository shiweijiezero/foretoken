// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"errors"
	"testing"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type failingCreateClient struct {
	client.Client
	err error
}

func (c failingCreateClient) Create(context.Context, client.Object, ...client.CreateOption) error {
	return c.err
}

func TestKVPoolReturnsMaterializationErrorsAfterStatusUpdate(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	pool := testKVPool(service)
	pool.Finalizers = []string{kvPoolFinalizer}
	baseClient := testKVClient(t, service, pool)
	applyErr := errors.New("create rejected")
	reconciler := &KVPoolReconciler{Client: failingCreateClient{Client: baseClient, err: applyErr}}
	_, err := reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)})
	if !errors.Is(err, applyErr) {
		t.Fatalf("Reconcile() error = %v, want materialization error", err)
	}
}

func TestKVGroupReturnsApplyErrorsAfterStatusUpdate(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	pool := testKVPool(service)
	spec, err := desiredKVGroupSpec(pool, service, 0)
	if err != nil {
		t.Fatal(err)
	}
	group := testKVGroup(pool, spec)
	group.Finalizers = []string{kvGroupFinalizer}
	baseClient := testKVClient(t, service, pool, group)
	applyErr := errors.New("apply rejected")
	reconciler := &KVGroupReconciler{Client: failingCreateClient{Client: baseClient, err: applyErr}}
	_, err = reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)})
	if !errors.Is(err, applyErr) {
		t.Fatalf("Reconcile() error = %v, want apply error", err)
	}
}

func TestKVPoolMaterializesRevisionedGroups(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	pool := testKVPool(service)
	kubeClient := testKVClient(t, service, pool)
	reconciler := &KVPoolReconciler{Client: kubeClient}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	var groups inferencev1alpha1.KVGroupList
	if err := kubeClient.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(groups.Items) != 2 {
		t.Fatalf("KVGroups = %#v", groups.Items)
	}
	for _, group := range groups.Items {
		if !metav1.IsControlledBy(&group, pool) || group.Spec.KVPoolRef.UID != string(pool.UID) || group.Spec.Ordinal < 0 || group.Spec.MasterRPCPort != 50051 {
			t.Fatalf("KVGroup = %#v", group)
		}
	}
}

func TestKVServiceScalesKVPoolInPlaceAndPreservesExistingGroups(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	kubeClient := testKVClient(t, service)
	serviceReconciler := &KVServiceReconciler{Client: kubeClient}
	serviceRequest := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	if _, err := serviceReconciler.Reconcile(ctx, serviceRequest); err != nil {
		t.Fatal(err)
	}
	if _, err := serviceReconciler.Reconcile(ctx, serviceRequest); err != nil {
		t.Fatal(err)
	}
	pool := new(inferencev1alpha1.KVPool)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: service.Namespace, Name: poolObjectName(service, "rdma")}, pool); err != nil {
		t.Fatal(err)
	}
	poolReconciler := &KVPoolReconciler{Client: kubeClient}
	poolRequest := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(pool)}
	if _, err := poolReconciler.Reconcile(ctx, poolRequest); err != nil {
		t.Fatal(err)
	}
	if _, err := poolReconciler.Reconcile(ctx, poolRequest); err != nil {
		t.Fatal(err)
	}
	var groups inferencev1alpha1.KVGroupList
	if err := kubeClient.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(groups.Items) != 2 {
		t.Fatalf("initial groups = %#v", groups.Items)
	}
	groupReconciler := &KVGroupReconciler{Client: kubeClient}
	preserved := map[int32]types.UID{}
	for index := range groups.Items {
		group := &groups.Items[index]
		preserved[group.Spec.Ordinal] = group.UID
		request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
		if _, err := groupReconciler.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
		if _, err := groupReconciler.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	currentService := new(inferencev1alpha1.KVService)
	if err := kubeClient.Get(ctx, serviceRequest.NamespacedName, currentService); err != nil {
		t.Fatal(err)
	}
	currentService.Spec.StoragePools[0].Replicas = 3
	currentService.Generation = 2
	if err := kubeClient.Update(ctx, currentService); err != nil {
		t.Fatal(err)
	}
	if _, err := serviceReconciler.Reconcile(ctx, serviceRequest); err != nil {
		t.Fatal(err)
	}
	currentPool := new(inferencev1alpha1.KVPool)
	if err := kubeClient.Get(ctx, poolRequest.NamespacedName, currentPool); err != nil {
		t.Fatal(err)
	}
	if currentPool.UID != pool.UID || currentPool.Spec.DesiredGroups != 3 {
		t.Fatalf("in-place scale replaced Pool: %#v", currentPool)
	}
	if _, err := poolReconciler.Reconcile(ctx, poolRequest); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(groups.Items) != 3 {
		t.Fatalf("scaled groups = %#v", groups.Items)
	}
	for _, group := range groups.Items {
		if ordinal := group.Spec.Ordinal; ordinal < 2 && group.UID != preserved[ordinal] {
			t.Fatalf("ordinal %d was replaced during scale-up", ordinal)
		}
	}
	for ordinal, uid := range preserved {
		pvcName := kvChildName(kvGroupObjectName(pool, currentPool.Spec.Revision, ordinal)+"-offload", string(uid))
		if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: service.Namespace, Name: pvcName}, new(corev1.PersistentVolumeClaim)); err != nil {
			t.Fatalf("ordinal %d PVC was not preserved: %v", ordinal, err)
		}
	}
	if err := kubeClient.Get(ctx, serviceRequest.NamespacedName, currentService); err != nil {
		t.Fatal(err)
	}
	currentService.Spec.StoragePools[0].Replicas = 1
	currentService.Generation = 3
	if err := kubeClient.Update(ctx, currentService); err != nil {
		t.Fatal(err)
	}
	if _, err := serviceReconciler.Reconcile(ctx, serviceRequest); err != nil {
		t.Fatal(err)
	}
	if _, err := poolReconciler.Reconcile(ctx, poolRequest); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		t.Fatal(err)
	}
	var ordinalZero *inferencev1alpha1.KVGroup
	for index := range groups.Items {
		group := &groups.Items[index]
		if group.Spec.Ordinal == 0 {
			ordinalZero = group
			continue
		}
		if group.Spec.Ordinal < 1 || group.DeletionTimestamp.IsZero() {
			t.Fatalf("scale-down did not delete only excess group: %#v", group)
		}
	}
	if ordinalZero == nil || ordinalZero.UID != preserved[0] {
		t.Fatalf("ordinal zero was not preserved during scale-down: %#v", groups.Items)
	}
}

func TestKVPoolReplacesRevisionAndScalesDownGroups(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	pool := testKVPool(service)
	pool.Spec.DesiredGroups = 1
	spec, err := desiredKVGroupSpec(pool, service, 0)
	if err != nil {
		t.Fatal(err)
	}
	staleSpec := spec
	staleSpec.Revision = "old-revision"
	stale := testKVGroup(pool, staleSpec)
	kubeClient := testKVClient(t, service, pool, stale)
	reconciler := &KVPoolReconciler{Client: kubeClient}
	state, err := reconciler.reconcileGroups(ctx, pool, service)
	if err != nil || state.materialized {
		t.Fatalf("revision replacement = (%#v, %v)", state, err)
	}
	state, err = reconciler.reconcileGroups(ctx, pool, service)
	if err != nil || !state.materialized {
		t.Fatalf("replacement convergence = (%#v, %v)", state, err)
	}
	pool.Spec.DesiredGroups = 0
	state, err = reconciler.reconcileGroups(ctx, pool, service)
	if err != nil || state.materialized {
		t.Fatalf("scale down = (%#v, %v)", state, err)
	}
}

func TestKVGroupMaterializesClientInfrastructure(t *testing.T) {
	ctx := context.Background()
	service := testKVService()
	pool := testKVPool(service)
	spec, err := desiredKVGroupSpec(pool, service, 0)
	if err != nil {
		t.Fatal(err)
	}
	spec.Timeouts.Drain = "10m"
	group := testKVGroup(pool, spec)
	kubeClient := testKVClient(t, service, pool, group)
	reconciler := &KVGroupReconciler{Client: kubeClient}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	deployment := new(appsv1.Deployment)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: kvGroupWorkloadName(group)}, deployment); err != nil {
		t.Fatal(err)
	}
	if deployment.Spec.Template.Spec.TerminationGracePeriodSeconds == nil || *deployment.Spec.Template.Spec.TerminationGracePeriodSeconds != 600 {
		t.Fatalf("terminationGracePeriodSeconds = %#v, want 600", deployment.Spec.Template.Spec.TerminationGracePeriodSeconds)
	}
	container := deployment.Spec.Template.Spec.Containers[0]
	for _, argument := range []string{"--host=$(POD_IP)", "--protocol=rdma", "--global_segment_size=536870912", "--enable_offload=true", "--metadata_server=P2PHANDSHAKE"} {
		if !containsArgument(container.Args, argument) {
			t.Fatalf("client args = %#v", container.Args)
		}
	}
	rdmaRequest := container.Resources.Requests[corev1.ResourceName("rdma/ib")]
	rdmaLimit := container.Resources.Limits[corev1.ResourceName("rdma/ib")]
	if container.Command[0] != "mooncake_client" || container.Env[0].ValueFrom == nil || rdmaRequest.Value() != 1 || rdmaLimit.Value() != 1 {
		t.Fatalf("client deployment = %#v", deployment.Spec)
	}
	pvc := new(corev1.PersistentVolumeClaim)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: kvChildName(group.Name+"-offload", string(group.UID))}, pvc); err != nil {
		t.Fatal(err)
	}
	if pvc.Annotations[kvGroupDiskRetentionAnnotation] != "Delete" || !metav1.IsControlledBy(pvc, group) {
		t.Fatalf("client PVC = %#v", pvc)
	}
	policy := new(networkingv1.NetworkPolicy)
	if err := kubeClient.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: kvGroupWorkloadName(group)}, policy); err != nil {
		t.Fatal(err)
	}
	if len(policy.Spec.Ingress) != 1 || len(policy.Spec.Ingress[0].From) != 1 {
		t.Fatalf("NetworkPolicy = %#v", policy.Spec)
	}
	current := new(inferencev1alpha1.KVGroup)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "ClientPodNotReady" {
		t.Fatalf("Ready = %#v", condition)
	}
}

func TestKVGroupDiskDeletionRespectsRecordedRetention(t *testing.T) {
	for _, retention := range []inferencev1alpha1.RetentionPolicy{inferencev1alpha1.RetentionPolicyDelete, inferencev1alpha1.RetentionPolicyRetain} {
		t.Run(string(retention), func(t *testing.T) {
			ctx := context.Background()
			service := testKVService()
			pool := testKVPool(service)
			spec, err := desiredKVGroupSpec(pool, service, 0)
			if err != nil {
				t.Fatal(err)
			}
			spec.Client.Disk.RetentionPolicy = retention
			group := testKVGroup(pool, spec)
			now := metav1.Now()
			group.DeletionTimestamp = &now
			group.Finalizers = []string{kvGroupFinalizer}
			pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: kvChildName(group.Name+"-offload", string(group.UID)), Namespace: group.Namespace, Annotations: map[string]string{kvGroupDiskRetentionAnnotation: string(retention)}, OwnerReferences: []metav1.OwnerReference{{UID: group.UID, Controller: ptr(true)}}}}
			kubeClient := testKVClient(t, group, pvc)
			reconciler := &KVGroupReconciler{Client: kubeClient}
			if _, err := reconciler.reconcileDelete(ctx, group); err != nil {
				t.Fatal(err)
			}
			current := new(corev1.PersistentVolumeClaim)
			err = kubeClient.Get(ctx, client.ObjectKeyFromObject(pvc), current)
			if retention == inferencev1alpha1.RetentionPolicyDelete {
				if !apierrors.IsNotFound(err) {
					t.Fatalf("Delete PVC get error = %v", err)
				}
			} else if err != nil || len(current.OwnerReferences) != 0 {
				t.Fatalf("Retain PVC = %#v, err = %v", current, err)
			}
		})
	}
}

func testKVPool(service *inferencev1alpha1.KVService) *inferencev1alpha1.KVPool {
	return &inferencev1alpha1.KVPool{ObjectMeta: metav1.ObjectMeta{Name: poolObjectName(service, "rdma"), Namespace: service.Namespace, UID: "pool-uid", OwnerReferences: []metav1.OwnerReference{{UID: service.UID, Controller: ptr(true)}}}, Spec: normalizedKVPoolSpec(service, service.Spec.StoragePools[0])}
}
func testKVGroup(pool *inferencev1alpha1.KVPool, spec inferencev1alpha1.KVGroupSpec) *inferencev1alpha1.KVGroup {
	return &inferencev1alpha1.KVGroup{ObjectMeta: metav1.ObjectMeta{Name: kvGroupObjectName(pool, spec.Revision, spec.Ordinal), Namespace: pool.Namespace, UID: types.UID("group-uid"), OwnerReferences: []metav1.OwnerReference{{UID: pool.UID, Controller: ptr(true)}}}, Spec: spec}
}
