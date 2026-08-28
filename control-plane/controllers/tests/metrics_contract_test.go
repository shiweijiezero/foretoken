// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package tests

import (
	"context"
	"testing"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/controllers"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type fixedPoolMetrics struct {
	observation core.DemandObservation
	targets     []core.TargetID
}

func (metrics *fixedPoolMetrics) Observation(_ context.Context, target core.TargetID) (core.DemandObservation, error) {
	metrics.targets = append(metrics.targets, target)
	return metrics.observation, nil
}

// TestStaleSourceMetricsStillEnforceMaximumCapacity protects hard capacity limits when telemetry becomes stale.
func TestStaleSourceMetricsStillEnforceMaximumCapacity(t *testing.T) {
	ctx := context.Background()
	service := modelService("bounded", 5)
	zero, maximum := int32(0), int32(3)
	service.Spec.Autoscaling = &inferencev1alpha1.ModelAutoscalingConfig{
		Algorithm:                   inferencev1alpha1.AutoscalingAlgorithmQueue,
		MinGroups:                   &zero,
		MaxGroups:                   &maximum,
		TargetQueuePerRoutableGroup: pointer(int64(1)),
	}
	planner, err := autoscaling.New(autoscaling.Configuration{DecisionAlgorithm: autoscaling.DecisionAlgorithmQueue, Decision: core.DecisionConfig{TargetQueuePerRoutableGroup: 1}})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	metrics := &fixedPoolMetrics{observation: core.DemandObservation{
		State:  core.ObservationFresh,
		Window: core.ObservationWindow{Start: now.Add(-time.Minute), End: now.Add(-time.Minute), CollectedAt: now, Samples: 1, Complete: true},
	}}
	c := controllerClient(t, service)
	r := &controllers.ModelServiceReconciler{Client: c, Autoscaler: planner, PoolMetricsProvider: metrics}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	for range 2 {
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	pool := get(t, ctx, c, client.ObjectKey{Namespace: service.Namespace, Name: "bounded-default"}, new(inferencev1alpha1.ModelPool))
	current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelService))
	if pool.Spec.DesiredGroups != maximum || len(current.Status.Autoscaling) != 1 {
		t.Fatalf("bounded pool/status = %#v %#v", pool.Spec, current.Status.Autoscaling)
	}
	status := current.Status.Autoscaling[0]
	if status.ObservationState != string(core.ObservationStale) || status.TriggerDisposition != string(core.TriggerInsufficientData) || status.Reason != string(core.DesiredCapacityReasonAtMaximum) || status.AdjustedGroups != maximum || status.AppliedGroups != maximum {
		t.Fatalf("bounded autoscaling status = %#v", status)
	}
}

// TestMetricsAggregationDrivesPoolScalingContract protects frontend telemetry aggregation through the ModelPool scaling result.
func TestMetricsAggregationDrivesPoolScalingContract(t *testing.T) {
	ctx := context.Background()
	service := modelService("autoscaled", 0)
	zero, maximum := int32(0), int32(2)
	service.Spec.Replicas = &zero
	service.Spec.Autoscaling = &inferencev1alpha1.ModelAutoscalingConfig{
		Algorithm:                   inferencev1alpha1.AutoscalingAlgorithmQueue,
		MinGroups:                   &zero,
		MaxGroups:                   &maximum,
		TargetQueuePerRoutableGroup: pointer(int64(0)),
	}
	planner, err := autoscaling.New(autoscaling.Configuration{
		DecisionAlgorithm: autoscaling.DecisionAlgorithmQueue,
		Decision:          core.DecisionConfig{TargetQueuePerRoutableGroup: 0},
	})
	if err != nil {
		t.Fatal(err)
	}
	now := time.Now()
	metrics := &fixedPoolMetrics{observation: core.DemandObservation{
		State:         core.ObservationFresh,
		Window:        core.ObservationWindow{Start: now.Add(-time.Second), End: now, CollectedAt: now, Samples: 1, Complete: true},
		QueueRequests: 1,
	}}
	c := controllerClient(t, service)
	r := &controllers.ModelServiceReconciler{Client: c, Autoscaler: planner, PoolMetricsProvider: metrics}
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	for range 2 {
		if _, err := r.Reconcile(ctx, request); err != nil {
			t.Fatal(err)
		}
	}
	pool := get(t, ctx, c, client.ObjectKey{Namespace: service.Namespace, Name: "autoscaled-default"}, new(inferencev1alpha1.ModelPool))
	if pool.Spec.DesiredGroups != 1 || len(metrics.targets) != 1 || metrics.targets[0].UID != string(pool.UID) {
		t.Fatalf("scaled pool and target attribution = pool %#v targets %#v", pool.Spec, metrics.targets)
	}
	current := get(t, ctx, c, request.NamespacedName, new(inferencev1alpha1.ModelService))
	if len(current.Status.Autoscaling) != 1 || current.Status.Autoscaling[0].ObservationState != string(core.ObservationFresh) || current.Status.Autoscaling[0].AppliedGroups != 1 {
		t.Fatalf("autoscaling aggregation status = %#v", current.Status.Autoscaling)
	}
}
