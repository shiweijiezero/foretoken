// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package controllers

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
)

type roundTripFunc func(*http.Request) (*http.Response, error)

func (roundTrip roundTripFunc) RoundTrip(request *http.Request) (*http.Response, error) {
	return roundTrip(request)
}

// TestHTTPScalingMetricsProviderUsesSchedulerBacklog protects waiting and running scheduler metrics in one snapshot.
func TestHTTPScalingMetricsProviderUsesSchedulerBacklog(t *testing.T) {
	const (
		namespace  = "serving"
		serviceUID = "service-uid"
		poolUID    = "pool-uid"
		revision   = "revision-1"
	)
	controller := true
	service := &inferencev1alpha1.ModelService{
		ObjectMeta: metav1.ObjectMeta{Name: "model", Namespace: namespace, UID: types.UID(serviceUID)},
		Status: inferencev1alpha1.ModelServiceStatus{ServingPoolRevisions: []inferencev1alpha1.ServingPoolRevision{{
			PoolName: "default", PoolUID: poolUID, Revision: revision,
		}}},
	}
	pool := &inferencev1alpha1.ModelPool{
		ObjectMeta: metav1.ObjectMeta{Name: "model-default", Namespace: namespace, UID: types.UID(poolUID)},
		Spec: inferencev1alpha1.ModelPoolSpec{
			ModelServiceRef: inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: serviceUID},
			PoolName:        "default",
			Template:        inferencev1alpha1.NormalizedPoolTemplate{Role: inferencev1alpha1.ModelRoleAggregate},
		},
	}
	group := &inferencev1alpha1.ModelGroup{
		ObjectMeta: metav1.ObjectMeta{
			Name:       "model-default-0",
			Namespace:  namespace,
			UID:        types.UID("group-uid"),
			Generation: 1,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelPool", Name: pool.Name, UID: pool.UID, Controller: &controller,
			}},
		},
		Spec: inferencev1alpha1.ModelGroupSpec{
			ModelPoolRef: inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: poolUID},
			Revision:     revision,
			MemberCount:  1,
			Runtime:      inferencev1alpha1.ModelGroupRuntime{Port: 9000},
		},
		Status: inferencev1alpha1.ModelGroupStatus{
			ReadyMembers: 1,
			Conditions: []metav1.Condition{{
				Type: conditionReady, Status: metav1.ConditionTrue, ObservedGeneration: 1,
			}},
		},
	}
	frontend := &corev1.Pod{
		ObjectMeta: metav1.ObjectMeta{Name: "frontend", Namespace: namespace, Labels: map[string]string{frontendServiceLabel: "frontend"}},
		Spec:       corev1.PodSpec{Containers: []corev1.Container{{Ports: []corev1.ContainerPort{{Name: frontendHTTPPortName, ContainerPort: 8080}}}}},
		Status:     corev1.PodStatus{PodIP: "127.0.0.1", Conditions: []corev1.PodCondition{{Type: corev1.PodReady, Status: corev1.ConditionTrue}}},
	}
	scheme := runtime.NewScheme()
	if err := inferencev1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	provider := NewHTTPScalingMetricsProvider(
		fake.NewClientBuilder().WithScheme(scheme).WithObjects(service, pool, group, frontend).Build(),
		AutoscalingTelemetryOptions{CollectionTimeout: time.Second, RequestTimeout: time.Second, Concurrency: 2},
	)
	provider.now = func() time.Time { return time.UnixMilli(2_000) }
	provider.httpClient.Transport = roundTripFunc(func(request *http.Request) (*http.Response, error) {
		body := `{"version":2,"collected_at_unix_ms":1000,"targets":[{"service_uid":"service-uid","target_kind":"Pool","target_id":"pool-uid","runtime_queued_requests":2,"dispatch_queued_requests":1}]}`
		if request.URL.Path == "/v1/internal/telemetry" {
			body = `{"version":2,"collected_at_unix_ms":900,"accepting":true,"running_requests":3,"max_concurrent_requests":256,"scheduler_running_requests":2,"scheduler_waiting_requests":5,"kv_cache_usage":0.5,"prompt_tokens_total":0,"generation_tokens_total":0,"ttft_seconds":{"count":0,"sum_seconds":0,"buckets":[]},"tpot_seconds":{"count":0,"sum_seconds":0,"buckets":[]},"e2e_seconds":{"count":0,"sum_seconds":0,"buckets":[]}}`
		}
		return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader(body)), Header: make(http.Header), Request: request}, nil
	})

	metrics, err := provider.Snapshot(context.Background(), core.TargetID{
		ServiceNamespace: namespace,
		ServiceName:      service.Name,
		ServiceUID:       serviceUID,
		Name:             pool.Spec.PoolName,
		UID:              poolUID,
		Kind:             core.TargetPool,
		Role:             core.RoleAggregate,
	})
	if err != nil {
		t.Fatal(err)
	}
	if metrics.WaitingRequests != 7 || metrics.RunningRequests != 2 || metrics.ActiveRequests != 3 || !metrics.Window.Complete || !metrics.Window.End.Equal(time.UnixMilli(900)) {
		t.Fatalf("metrics = %#v", metrics)
	}
}
