// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package tests

import (
	"context"
	"testing"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type fixedScalingMetrics struct {
	snapshot core.MetricsSnapshot
	targets  []core.TargetID
}

func (metrics *fixedScalingMetrics) Snapshot(_ context.Context, target core.TargetID) (core.MetricsSnapshot, error) {
	metrics.targets = append(metrics.targets, target)
	return metrics.snapshot, nil
}

// TestStaleSourceMetricsStillEnforceMaximumCapacity protects hard capacity limits when telemetry becomes stale.
func TestStaleSourceMetricsStillEnforceMaximumCapacity(t *testing.T) {
	ctx := context.Background()
	service := modelService("bounded", 5)
	service.Spec.Autoscaling = &inferencev1alpha1.ModelAutoscalingConfig{
		MinReplicas: 1,
		MaxReplicas: 3,
		Decision: inferencev1alpha1.ModelAutoscalingDecisionConfig{
			Algorithm: inferencev1alpha1.AutoscalingDecisionAlgorithmQueue,
			Queue: &inferencev1alpha1.ModelAutoscalingQueueDecisionConfig{
				TargetAverageQueuedRequests: pointer(int64(1)),
			},
		},
	}
	now := time.Now()
	metrics := &fixedScalingMetrics{snapshot: core.MetricsSnapshot{
		State:  core.MetricsFresh,
		Window: core.MetricsWindow{Start: now.Add(-time.Minute), End: now.Add(-time.Minute), CollectedAt: now, Samples: 1, Complete: true},
	}}
	c := controllerClient(t, service)
	r := &controllers.ModelServiceReconciler{Client: c, MetricsProvider: metrics}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	for range 2 {
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	pool := get(t, ctx, c, client.ObjectKey{Namespace: service.Namespace, Name: "bounded-default"}, new(inferencev1alpha1.ModelPool))
	current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelService))
	if pool.Spec.DesiredGroups != 3 || len(current.Status.Autoscaling) != 1 {
		t.Fatalf("bounded pool/status = %#v %#v", pool.Spec, current.Status.Autoscaling)
	}
	status := current.Status.Autoscaling[0]
	if status.ObservationState != string(core.MetricsStale) || status.Trigger == nil || status.Trigger.Disposition != string(core.TriggerInsufficientData) || status.Constraint == nil || status.Constraint.Reason != string(core.ConstraintReasonAtMaximum) || status.Adjustment.AdjustedReplicas != 3 || status.AppliedReplicas != 3 {
		t.Fatalf("bounded autoscaling status = %#v", status)
	}
}

// TestMetricsAggregationDrivesPoolScalingContract protects frontend telemetry aggregation through the ModelPool scaling result.
func TestMetricsAggregationDrivesPoolScalingContract(t *testing.T) {
	ctx := context.Background()
	service := modelService("autoscaled", 1)
	service.Spec.Autoscaling = &inferencev1alpha1.ModelAutoscalingConfig{
		MinReplicas: 1,
		MaxReplicas: 2,
		Decision: inferencev1alpha1.ModelAutoscalingDecisionConfig{
			Algorithm: inferencev1alpha1.AutoscalingDecisionAlgorithmQueue,
			Queue: &inferencev1alpha1.ModelAutoscalingQueueDecisionConfig{
				TargetAverageQueuedRequests: pointer(int64(1)),
			},
		},
	}
	now := time.Now()
	metrics := &fixedScalingMetrics{snapshot: core.MetricsSnapshot{
		State:           core.MetricsFresh,
		Window:          core.MetricsWindow{Start: now.Add(-time.Second), End: now, CollectedAt: now, Samples: 1, Complete: true},
		WaitingRequests: 2,
	}}
	c := controllerClient(t, service)
	r := &controllers.ModelServiceReconciler{Client: c, MetricsProvider: metrics}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	for range 2 {
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	pool := get(t, ctx, c, client.ObjectKey{Namespace: service.Namespace, Name: "autoscaled-default"}, new(inferencev1alpha1.ModelPool))
	if pool.Spec.DesiredGroups != 2 || len(metrics.targets) != 1 || metrics.targets[0].UID != string(pool.UID) {
		t.Fatalf("scaled pool and target attribution = pool %#v targets %#v", pool.Spec, metrics.targets)
	}
	current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelService))
	if len(current.Status.Autoscaling) != 1 || current.Status.Autoscaling[0].ObservationState != string(core.MetricsFresh) || current.Status.Autoscaling[0].Decision.DesiredReplicas != 2 || current.Status.Autoscaling[0].AppliedReplicas != 2 {
		t.Fatalf("autoscaling aggregation status = %#v", current.Status.Autoscaling)
	}
}
