// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Tests route withdrawal, admission close, and bounded ModelGroup deletion.

package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"testing"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type fakeModelGroupDrainClient struct {
	frontendGeneration uint64
	frontendErr        error
	blockFrontend      bool
	telemetry          drainTelemetry
	closeErr           error
	frontendCalls      int
	closeCalls         int
}

func (client *fakeModelGroupDrainClient) FrontendGeneration(ctx context.Context, _ string) (uint64, error) {
	client.frontendCalls++
	if client.blockFrontend {
		<-ctx.Done()
		return 0, ctx.Err()
	}
	return client.frontendGeneration, client.frontendErr
}

func (client *fakeModelGroupDrainClient) CloseAdmission(context.Context, string) (drainTelemetry, error) {
	client.closeCalls++
	return client.telemetry, client.closeErr
}

func TestModelGroupDeleteWaitsForWithdrawalAndRunningRequests(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)
	_, group := testDeletingModelGroup("draining-group", now)
	frontend := testFrontendService("frontend")
	snapshot := testServingSnapshotConfigMap(t, frontend, servingSnapshot{
		Version: 7,
		Groups:  []servingSnapshotGroup{{RouteTargetID: string(group.UID)}},
	})
	pod := testReadyFrontendPod(frontend, "10.0.0.8")
	drainClient := &fakeModelGroupDrainClient{
		frontendGeneration: 6,
		telemetry: drainTelemetry{
			Version:         modelServerTelemetryVersion,
			Accepting:       false,
			RunningRequests: 2,
		},
	}
	reconciler, kubeClient := testGroupReconciler(t, group, frontend, snapshot, pod)
	reconciler.DrainClient = drainClient
	reconciler.Now = func() time.Time { return now }
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}

	reconcileModelGroup(t, ctx, reconciler, request)
	current := getModelGroup(t, ctx, kubeClient, request.NamespacedName)
	if current.Status.DrainStartedAt == nil || !current.Status.DrainStartedAt.Time.Equal(now) {
		t.Fatalf("drain start = %#v", current.Status.DrainStartedAt)
	}
	if drainClient.frontendCalls != 0 || drainClient.closeCalls != 1 {
		t.Fatalf("admission was not closed before route withdrawal: frontend=%d close=%d", drainClient.frontendCalls, drainClient.closeCalls)
	}

	snapshot.Data[servingSnapshotKey] = encodeServingSnapshot(t, servingSnapshot{Version: 7})
	if err := kubeClient.Update(ctx, snapshot); err != nil {
		t.Fatal(err)
	}
	reconcileModelGroup(t, ctx, reconciler, request)
	if drainClient.frontendCalls != 1 || drainClient.closeCalls != 2 {
		t.Fatalf("unacknowledged generation advanced drain: frontend=%d close=%d", drainClient.frontendCalls, drainClient.closeCalls)
	}

	drainClient.frontendGeneration = 7
	reconcileModelGroup(t, ctx, reconciler, request)
	current = getModelGroup(t, ctx, kubeClient, request.NamespacedName)
	if drainClient.closeCalls != 3 {
		t.Fatalf("admission close calls = %d", drainClient.closeCalls)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionAdmissionClosed); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("AdmissionClosed condition = %#v", condition)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionDrained); condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "RequestsRunning" {
		t.Fatalf("Drained condition = %#v", condition)
	}

	drainClient.telemetry.RunningRequests = 0
	reconcileModelGroup(t, ctx, reconciler, request)
	assertModelGroupDeleted(t, ctx, kubeClient, request.NamespacedName)
}

func TestModelGroupDeleteRetriesAdmissionCloseFailure(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)
	_, group := testDeletingModelGroup("close-failure-group", now)
	frontend := testFrontendService("frontend")
	snapshot := testServingSnapshotConfigMap(t, frontend, servingSnapshot{Version: 4})
	drainClient := &fakeModelGroupDrainClient{closeErr: errors.New("model-server unavailable")}
	reconciler, kubeClient := testGroupReconciler(t, group, frontend, snapshot)
	reconciler.DrainClient = drainClient
	reconciler.Now = func() time.Time { return now }
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}

	reconcileModelGroup(t, ctx, reconciler, request)
	current := getModelGroup(t, ctx, kubeClient, request.NamespacedName)
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionAdmissionClosed); condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "AdmissionCloseFailed" {
		t.Fatalf("AdmissionClosed condition = %#v", condition)
	}
	if !containsString(current.Finalizers, modelGroupDrainFinalizer) {
		t.Fatalf("finalizers = %#v", current.Finalizers)
	}
}

func TestModelGroupDeleteDrainsWithoutFrontendServices(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)
	_, group := testDeletingModelGroup("no-frontend-group", now)
	drainClient := &fakeModelGroupDrainClient{telemetry: drainTelemetry{
		Version:   modelServerTelemetryVersion,
		Accepting: false,
	}}
	reconciler, kubeClient := testGroupReconciler(t, group)
	reconciler.DrainClient = drainClient
	reconciler.Now = func() time.Time { return now }
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}

	reconcileModelGroup(t, ctx, reconciler, request)
	if drainClient.closeCalls != 1 || drainClient.frontendCalls != 0 {
		t.Fatalf("drain calls = close %d, frontend %d", drainClient.closeCalls, drainClient.frontendCalls)
	}
	assertModelGroupDeleted(t, ctx, kubeClient, request.NamespacedName)
}

func TestModelGroupDeleteRemovesFinalizerAtDrainDeadline(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 8, 7, 12, 3, 0, 0, time.UTC)
	_, group := testDeletingModelGroup("timeout-group", now)
	started := metav1.NewTime(now.Add(-2 * time.Minute))
	group.Status.DrainStartedAt = &started
	drainClient := &fakeModelGroupDrainClient{}
	reconciler, kubeClient := testGroupReconciler(t, group)
	reconciler.DrainClient = drainClient
	reconciler.Now = func() time.Time { return now }
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}

	reconcileModelGroup(t, ctx, reconciler, request)
	if drainClient.frontendCalls != 0 || drainClient.closeCalls != 0 {
		t.Fatalf("deadline path contacted drain endpoints: frontend=%d close=%d", drainClient.frontendCalls, drainClient.closeCalls)
	}
	assertModelGroupDeleted(t, ctx, kubeClient, request.NamespacedName)
}

func TestModelGroupDeleteBoundsFrontendObservationByDrainDeadline(t *testing.T) {
	ctx := context.Background()
	now := time.Date(2026, 8, 7, 12, 0, 0, 0, time.UTC)
	_, group := testDeletingModelGroup("observation-timeout-group", now)
	group.Spec.Timeouts.Drain = "10ms"
	started := metav1.NewTime(now)
	group.Status.DrainStartedAt = &started
	frontend := testFrontendService("frontend")
	snapshot := testServingSnapshotConfigMap(t, frontend, servingSnapshot{Version: 2})
	pod := testReadyFrontendPod(frontend, "10.0.0.9")
	drainClient := &fakeModelGroupDrainClient{
		blockFrontend: true,
		telemetry: drainTelemetry{
			Version:   modelServerTelemetryVersion,
			Accepting: false,
		},
	}
	reconciler, kubeClient := testGroupReconciler(t, group, frontend, snapshot, pod)
	reconciler.DrainClient = drainClient
	reconciler.Now = func() time.Time { return now }
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(group)}

	reconcileModelGroup(t, ctx, reconciler, request)
	if drainClient.closeCalls != 1 || drainClient.frontendCalls != 1 {
		t.Fatalf("drain calls = close %d, frontend %d", drainClient.closeCalls, drainClient.frontendCalls)
	}
	assertModelGroupDeleted(t, ctx, kubeClient, request.NamespacedName)
}

func testDeletingModelGroup(name string, now time.Time) (*inferencev1alpha1.ModelPool, *inferencev1alpha1.ModelGroup) {
	pool, group := testModelGroup(name)
	deleting := metav1.NewTime(now.Add(-time.Minute))
	group.DeletionTimestamp = &deleting
	group.Finalizers = []string{modelGroupDrainFinalizer}
	return pool, group
}

func testServingSnapshotConfigMap(t *testing.T, frontend *inferencev1alpha1.FrontendService, snapshot servingSnapshot) *corev1.ConfigMap {
	t.Helper()
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: frontendServingConfigMapName(frontend), Namespace: frontend.Namespace},
		Data:       map[string]string{servingSnapshotKey: encodeServingSnapshot(t, snapshot)},
	}
}

func encodeServingSnapshot(t *testing.T, snapshot servingSnapshot) string {
	t.Helper()
	payload, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	return string(payload)
}

func testReadyFrontendPod(frontend *inferencev1alpha1.FrontendService, ip string) *corev1.Pod {
	return &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: frontend.Name + "-pod", Namespace: frontend.Namespace, Labels: map[string]string{frontendServiceLabel: frontend.Name}},
		Spec: corev1.PodSpec{Containers: []corev1.Container{{
			Name:  "frontend",
			Ports: []corev1.ContainerPort{{Name: frontendHTTPPortName, ContainerPort: 8080}},
		}}},
		Status: corev1.PodStatus{
			PodIP: ip,
			Conditions: []corev1.PodCondition{{
				Type: corev1.PodReady, Status: corev1.ConditionTrue,
			}},
		},
	}
}

func getModelGroup(t *testing.T, ctx context.Context, kubeClient client.Client, key client.ObjectKey) *inferencev1alpha1.ModelGroup {
	t.Helper()
	group := new(inferencev1alpha1.ModelGroup)
	if err := kubeClient.Get(ctx, key, group); err != nil {
		t.Fatal(err)
	}
	return group
}

func assertModelGroupDeleted(t *testing.T, ctx context.Context, kubeClient client.Client, key client.ObjectKey) {
	t.Helper()
	err := kubeClient.Get(ctx, key, new(inferencev1alpha1.ModelGroup))
	if !apierrors.IsNotFound(err) {
		t.Fatalf("ModelGroup still exists: %v", err)
	}
}

func containsString(values []string, expected string) bool {
	for _, value := range values {
		if value == expected {
			return true
		}
	}
	return false
}
