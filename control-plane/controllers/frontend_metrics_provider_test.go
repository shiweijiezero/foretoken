// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (function roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return function(request)
}

func TestHTTPPoolMetricsProviderAggregatesCompleteTargetObservation(t *testing.T) {
	now := time.Now().Truncate(time.Millisecond)
	service := testModelService("observed", 1)
	pool := readyPool(service, "default", inferencev1alpha1.ModelRoleAggregate, 1, "r1")
	group := readyGroup(pool, inferencev1alpha1.ModelRoleAggregate, "r1", 0)
	group.Spec.Runtime.Port = 9000
	frontend := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "frontend-0", Namespace: service.Namespace, Labels: map[string]string{frontendServiceLabel: "frontend"}},
		Status:     corev1.PodStatus{PodIP: "127.0.0.1", Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "frontend", Ports: []corev1.ContainerPort{{Name: frontendHTTPPortName, ContainerPort: 8080}}}}},
	}
	reconciler, kubeClient := testReconciler(t, service, pool, group, frontend)
	_ = reconciler
	provider := NewHTTPPoolMetricsProvider(kubeClient)
	provider.now = func() time.Time { return now }
	provider.httpClient = &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		body := `{"version":1,"accepting":true,"running_requests":3,"max_concurrent_requests":8}`
		if request.URL.Path == "/internal/autoscaling/telemetry" {
			body = `{"version":1,"collected_at_unix_ms":` + strings.TrimSpace(timeMillis(now)) + `,"targets":[{"service_uid":"` + string(service.UID) + `","target_kind":"Pool","target_id":"` + string(pool.UID) + `","queued_requests":2}]}`
		}
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
	})}

	observation, err := provider.Observation(context.Background(), algorithm.TargetID{
		ServiceNamespace: service.Namespace,
		ServiceName:      service.Name,
		ServiceUID:       string(service.UID),
		Name:             "default",
		UID:              string(pool.UID),
		Kind:             algorithm.TargetPool,
		Role:             algorithm.RoleAggregate,
	})
	if err != nil {
		t.Fatal(err)
	}
	if observation.State != algorithm.ObservationFresh || !observation.Window.Complete || observation.QueueRequests != 2 || observation.ActiveRequests != 3 {
		t.Fatalf("observation = %#v", observation)
	}
}

func TestFrontendQueueTreatsAnAbsentPodTargetAsZero(t *testing.T) {
	now := time.Now().Truncate(time.Millisecond)
	service := testModelService("rollout", 1)
	frontend := func(name, ip string) *corev1.Pod {
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: service.Namespace, Labels: map[string]string{frontendServiceLabel: "frontend"}},
			Status:     corev1.PodStatus{PodIP: ip, Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}},
			Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "frontend", Ports: []corev1.ContainerPort{{Name: frontendHTTPPortName, ContainerPort: 8080}}}}},
		}
	}
	_, kubeClient := testReconciler(t, service, frontend("frontend-0", "127.0.0.1"), frontend("frontend-1", "127.0.0.2"))
	provider := NewHTTPPoolMetricsProvider(kubeClient)
	provider.httpClient = &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		targets := `[]`
		if request.URL.Hostname() == "127.0.0.1" {
			targets = `[{"service_uid":"` + string(service.UID) + `","target_kind":"Pool","target_id":"pool-uid","queued_requests":2}]`
		}
		body := `{"version":1,"collected_at_unix_ms":` + strings.TrimSpace(timeMillis(now)) + `,"targets":` + targets + `}`
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header)}, nil
	})}

	queue, samples, observedAt, err := provider.frontendQueue(context.Background(), algorithm.TargetID{
		ServiceNamespace: service.Namespace,
		ServiceUID:       string(service.UID),
		Name:             "default",
		UID:              "pool-uid",
		Kind:             algorithm.TargetPool,
	})
	if err != nil {
		t.Fatal(err)
	}
	if queue != 2 || samples != 2 || !observedAt.Equal(now) {
		t.Fatalf("queue observation = (%d, %d, %s)", queue, samples, observedAt)
	}
}

func TestTelemetryTargetMatchesOpaquePoolAndEPDIdentities(t *testing.T) {
	values := []frontendAutoscalingTarget{
		{ServiceUID: "service", TargetKind: "Pool", TargetID: "pool", QueuedRequests: 2},
		{ServiceUID: "service", TargetKind: "EPDDomain", TargetID: "service", QueuedRequests: 3},
	}
	if value, found := telemetryTarget(values, algorithm.TargetID{ServiceUID: "service", UID: "pool", Kind: algorithm.TargetPool}); !found || value != 2 {
		t.Fatalf("Pool target = (%d, %v)", value, found)
	}
	if value, found := telemetryTarget(values, algorithm.TargetID{ServiceUID: "service", Name: "epd", Kind: algorithm.TargetEPDDomain}); !found || value != 3 {
		t.Fatalf("E/P/D target = (%d, %v)", value, found)
	}
}

func TestHTTPPoolMetricsProviderHonorsCollectionContext(t *testing.T) {
	service := testModelService("timeout", 1)
	pool := readyPool(service, "default", inferencev1alpha1.ModelRoleAggregate, 1, "r1")
	group := readyGroup(pool, inferencev1alpha1.ModelRoleAggregate, "r1", 0)
	group.Spec.Runtime.Port = 9000
	frontend := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "frontend-0", Namespace: service.Namespace, Labels: map[string]string{frontendServiceLabel: "frontend"}},
		Status:     corev1.PodStatus{PodIP: "127.0.0.1", Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Name: "frontend", Ports: []corev1.ContainerPort{{Name: frontendHTTPPortName, ContainerPort: 8080}}}}},
	}
	_, kubeClient := testReconciler(t, service, pool, group, frontend)
	provider := NewHTTPPoolMetricsProvider(kubeClient)
	provider.httpClient = &http.Client{Transport: roundTripFunc(func(request *http.Request) (*http.Response, error) {
		<-request.Context().Done()
		return nil, request.Context().Err()
	})}

	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	startedAt := time.Now()
	_, err := provider.Observation(ctx, algorithm.TargetID{
		ServiceNamespace: service.Namespace,
		ServiceName:      service.Name,
		ServiceUID:       string(service.UID),
		Name:             "default",
		UID:              string(pool.UID),
		Kind:             algorithm.TargetPool,
		Role:             algorithm.RoleAggregate,
	})
	if err == nil {
		t.Fatal("collection succeeded after its context expired")
	}
	if elapsed := time.Since(startedAt); elapsed > time.Second {
		t.Fatalf("collection ignored cancellation for %s", elapsed)
	}
}

func timeMillis(value time.Time) string {
	return fmt.Sprintf("%d", value.UnixMilli())
}
