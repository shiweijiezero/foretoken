// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Tests ModelService reconciliation, ownership, status, and deletion behavior.

package controllers

import (
	"context"
	"reflect"
	"testing"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

type fixedScalingAlgorithm struct {
	desired int32
}

func (fixedScalingAlgorithm) Name() string { return "test" }

func (fixed fixedScalingAlgorithm) Recommend(_ context.Context, snapshot algorithm.Snapshot) (algorithm.Recommendation, error) {
	return algorithm.Recommendation{
		Target:        snapshot.Target,
		SnapshotID:    snapshot.Ref.ID,
		Disposition:   algorithm.RecommendationApply,
		DesiredGroups: fixed.desired,
		Reason:        algorithm.ReasonStable,
	}, nil
}

type currentCapacityAlgorithm struct{}

func (currentCapacityAlgorithm) Name() string { return "current" }
func (currentCapacityAlgorithm) Recommend(_ context.Context, snapshot algorithm.Snapshot) (algorithm.Recommendation, error) {
	return algorithm.Recommendation{Target: snapshot.Target, SnapshotID: snapshot.Ref.ID, Disposition: algorithm.RecommendationHold, DesiredGroups: snapshot.Capacity.RequestedGroups, Reason: algorithm.ReasonStable}, nil
}

func TestNewPoolSnapshotStartsFromCompiledCapacity(t *testing.T) {
	ctx := context.Background()
	service := testModelService("initial", 1)
	reconciler, kubeClient := testReconciler(t, service)
	reconciler.Autoscaler = autoscaling.NewWithAlgorithm(currentCapacityAlgorithm{})
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	reconcileTwice(t, ctx, reconciler, request)

	pool := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: "initial-default"}, pool); err != nil {
		t.Fatal(err)
	}
	if pool.Spec.DesiredGroups != 1 {
		t.Fatalf("initial desiredGroups = %d, want compiled baseline 1", pool.Spec.DesiredGroups)
	}
}

func TestModelServiceReconcileMaterializesPool(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	reconciler, kubeClient := testReconciler(t, service)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}

	result, err := reconciler.Reconcile(ctx, request)
	if err != nil || !result.Requeue {
		t.Fatalf("first Reconcile() = (%#v, %v), want requeue without error", result, err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}

	pool := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: "demo-default"}, pool); err != nil {
		t.Fatal(err)
	}
	if pool.Spec.ModelServiceRef.Name != service.Name || pool.Spec.ModelServiceRef.UID != string(service.UID) {
		t.Fatalf("ModelService reference = %#v", pool.Spec.ModelServiceRef)
	}
	if pool.Spec.PoolName != "default" || pool.Spec.DesiredGroups != 1 {
		t.Fatalf("ModelPool spec = %#v", pool.Spec)
	}
	if !metav1.IsControlledBy(pool, service) {
		t.Fatal("ModelPool is not controlled by ModelService")
	}

	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if current.Status.ObservedGeneration != service.Generation {
		t.Fatalf("observedGeneration = %d, want %d", current.Status.ObservedGeneration, service.Generation)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionPoolsMaterialized); condition == nil || condition.Status != metav1.ConditionTrue {
		t.Fatalf("PoolsMaterialized condition = %#v", condition)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelServiceAppliesInjectedAutoscalingDecisionWithoutChangingTemplate(t *testing.T) {
	ctx := context.Background()
	service := testModelService("autoscaled", 1)
	reconciler, kubeClient := testReconciler(t, service)
	reconciler.Autoscaler = autoscaling.NewWithAlgorithm(fixedScalingAlgorithm{desired: 2})
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	reconcileTwice(t, ctx, reconciler, request)

	pool := new(inferencev1alpha1.ModelPool)
	if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: "autoscaled-default"}, pool); err != nil {
		t.Fatal(err)
	}
	if pool.Spec.DesiredGroups != 2 {
		t.Fatalf("desiredGroups = %d, want 2", pool.Spec.DesiredGroups)
	}
	if pool.Spec.Template.Model != service.Spec.Model || pool.Spec.Template.Role != inferencev1alpha1.ModelRoleAggregate {
		t.Fatalf("autoscaler changed Pool template = %#v", pool.Spec.Template)
	}
	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if len(current.Status.Autoscaling) != 1 {
		t.Fatalf("autoscaling status = %#v", current.Status.Autoscaling)
	}
	status := current.Status.Autoscaling[0]
	if status.ID != "Pool/default" || status.Algorithm != "test" || status.RequestedGroups != 2 || status.AppliedGroups != 2 || status.ObservationState != "Unavailable" {
		t.Fatalf("autoscaling status = %#v", status)
	}
}

func TestPDAutoscalingAppliesIndependentPoolDecisions(t *testing.T) {
	ctx := context.Background()
	service := testModelService("pd-autoscaled", 1)
	service.Spec.Resources = nil
	service.Spec.Parallelism = nil
	one := int32(1)
	poolTemplate := func(name string, role inferencev1alpha1.ModelRole) inferencev1alpha1.ModelPoolTemplate {
		return inferencev1alpha1.ModelPoolTemplate{
			Name:     name,
			Role:     role,
			Replicas: &one,
			Resources: inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
				ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
				GPU:                     inferencev1alpha1.GPURequest{Type: "auto", Count: 1},
			}},
			Parallelism: inferencev1alpha1.Parallelism{TP: 1, PP: 1, PCP: 1, DCP: 1},
		}
	}
	service.Spec.ModelPools = []inferencev1alpha1.ModelPoolTemplate{
		poolTemplate("prefill", inferencev1alpha1.ModelRolePrefill),
		poolTemplate("decode", inferencev1alpha1.ModelRoleDecode),
	}
	reconciler, kubeClient := testReconciler(t, service)
	reconciler.Autoscaler = autoscaling.NewWithAlgorithm(fixedScalingAlgorithm{desired: 2})
	reconcileTwice(t, ctx, reconciler, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)})

	for _, name := range []string{"prefill", "decode"} {
		pool := new(inferencev1alpha1.ModelPool)
		if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: service.Name + "-" + name}, pool); err != nil {
			t.Fatal(err)
		}
		if pool.Spec.DesiredGroups != 2 {
			t.Fatalf("%s desiredGroups = %d, want 2", name, pool.Spec.DesiredGroups)
		}
	}
}

func TestEPDAutoscalingAppliesOneDomainDecisionToEveryRole(t *testing.T) {
	ctx := context.Background()
	service := testModelService("epd-autoscaled", 1)
	service.Spec.Resources = nil
	service.Spec.Parallelism = nil
	service.Spec.ECProfile = &inferencev1alpha1.ECProfileReference{Profile: "verified-ec"}
	two := int32(2)
	poolTemplate := func(name string, role inferencev1alpha1.ModelRole) inferencev1alpha1.ModelPoolTemplate {
		return inferencev1alpha1.ModelPoolTemplate{
			Name:     name,
			Role:     role,
			Replicas: &two,
			Resources: inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
				ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
				GPU:                     inferencev1alpha1.GPURequest{Type: "auto", Count: 1},
			}},
			Parallelism: inferencev1alpha1.Parallelism{TP: 1, PP: 1, PCP: 1, DCP: 1},
		}
	}
	service.Spec.ModelPools = []inferencev1alpha1.ModelPoolTemplate{
		poolTemplate("encoder", inferencev1alpha1.ModelRoleEncoder),
		poolTemplate("prefill", inferencev1alpha1.ModelRolePrefill),
		poolTemplate("decode", inferencev1alpha1.ModelRoleDecode),
	}
	reconciler, kubeClient := testReconciler(t, service)
	reconcileTwice(t, ctx, reconciler, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)})

	for _, name := range []string{"encoder", "prefill", "decode"} {
		pool := new(inferencev1alpha1.ModelPool)
		if err := kubeClient.Get(ctx, types.NamespacedName{Namespace: service.Namespace, Name: service.Name + "-" + name}, pool); err != nil {
			t.Fatal(err)
		}
		if pool.Spec.DesiredGroups != 2 {
			t.Fatalf("%s desiredGroups = %d, want 2", name, pool.Spec.DesiredGroups)
		}
	}
}

func TestEPDScalingRecoversFromPartialDesiredGroupWrites(t *testing.T) {
	service := testModelService("epd-recovery", 1)
	pools := []compiler.ModelPool{
		{Name: "encoder", DesiredGroups: 1, Template: inferencev1alpha1.NormalizedPoolTemplate{Role: inferencev1alpha1.ModelRoleEncoder}},
		{Name: "prefill", DesiredGroups: 1, Template: inferencev1alpha1.NormalizedPoolTemplate{Role: inferencev1alpha1.ModelRolePrefill}},
		{Name: "decode", DesiredGroups: 1, Template: inferencev1alpha1.NormalizedPoolTemplate{Role: inferencev1alpha1.ModelRoleDecode}},
	}
	owned := map[string]*inferencev1alpha1.ModelPool{
		"encoder": {Spec: inferencev1alpha1.ModelPoolSpec{DesiredGroups: 2}},
		"prefill": {Spec: inferencev1alpha1.ModelPoolSpec{DesiredGroups: 1}},
		"decode":  {Spec: inferencev1alpha1.ModelPoolSpec{DesiredGroups: 1}},
	}
	snapshot, err := epdScalingSnapshot(service, pools, []int{0, 1, 2}, owned, "1", metav1.Now())
	if err != nil {
		t.Fatal(err)
	}
	if snapshot.Capacity.RequestedGroups != 2 {
		t.Fatalf("recovery requestedGroups = %d, want highest partial request 2", snapshot.Capacity.RequestedGroups)
	}
}

func TestAutoscalingTreatsReadyRolloutAsTransitioning(t *testing.T) {
	pool := &inferencev1alpha1.ModelPool{ObjectMeta: metav1.ObjectMeta{Generation: 2}, Status: inferencev1alpha1.ModelPoolStatus{ObservedGeneration: 2}}
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionReady, Status: metav1.ConditionTrue, ObservedGeneration: 2, Reason: "ActiveRevisionReady"})
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionRolloutPending, Status: metav1.ConditionTrue, ObservedGeneration: 2, Reason: "Converging"})
	if !modelPoolTransitioning(pool) {
		t.Fatal("ready Pool with a pending rollout was treated as stable capacity")
	}
}

func TestModelServiceBecomesReadyWhenAllServingPoolsAreReady(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	reconciler, kubeClient := testReconciler(t, service)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	reconcileTwice(t, ctx, reconciler, request)

	pool := new(inferencev1alpha1.ModelPool)
	poolKey := types.NamespacedName{Namespace: service.Namespace, Name: "demo-default"}
	if err := kubeClient.Get(ctx, poolKey, pool); err != nil {
		t.Fatal(err)
	}
	pool.Status.ObservedGeneration = pool.Generation
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{
		Type:               conditionReady,
		Status:             metav1.ConditionTrue,
		Reason:             "Ready",
		Message:            "Ready",
		ObservedGeneration: pool.Generation,
	})
	if err := kubeClient.Status().Update(ctx, pool); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}

	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady)
	if condition == nil || condition.Status != metav1.ConditionTrue || condition.Reason != "Ready" || condition.ObservedGeneration != current.Generation {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelServiceScaledToZeroIsNotServingReady(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	service.Spec.Replicas = ptr(int32(0))
	reconciler, kubeClient := testReconciler(t, service)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	reconcileTwice(t, ctx, reconciler, request)

	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	condition := meta.FindStatusCondition(current.Status.Conditions, conditionReady)
	if condition == nil || condition.Status != metav1.ConditionFalse || condition.Reason != "ScaledToZero" {
		t.Fatalf("Ready condition = %#v", condition)
	}
}

func TestModelServiceScalePreservesTemplate(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	reconciler, kubeClient := testReconciler(t, service)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	reconcileTwice(t, ctx, reconciler, request)

	pool := new(inferencev1alpha1.ModelPool)
	poolKey := types.NamespacedName{Namespace: service.Namespace, Name: "demo-default"}
	if err := kubeClient.Get(ctx, poolKey, pool); err != nil {
		t.Fatal(err)
	}
	originalTemplate := pool.Spec.Template.DeepCopy()

	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	replicas := int32(2)
	current.Spec.Replicas = &replicas
	current.Generation = 4
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, poolKey, pool); err != nil {
		t.Fatal(err)
	}
	if pool.Spec.DesiredGroups != 2 || !reflect.DeepEqual(&pool.Spec.Template, originalTemplate) {
		t.Fatalf("scale changed Pool template: %#v", pool.Spec)
	}

	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	current.Spec.Resources.Requests.CPU = "2"
	current.Generation = 5
	if err := kubeClient.Update(ctx, current); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if err := kubeClient.Get(ctx, poolKey, pool); err != nil {
		t.Fatal(err)
	}
	if reflect.DeepEqual(&pool.Spec.Template, originalTemplate) || pool.Spec.Template.Resources.Requests.CPU != "2" {
		t.Fatalf("config change did not update Pool template: %#v", pool.Spec.Template)
	}
}

func TestModelServiceInvalidIntentDoesNotCreatePool(t *testing.T) {
	ctx := context.Background()
	service := testModelService("invalid", 2)
	service.Spec.Parallelism.TP = 2
	reconciler, kubeClient := testReconciler(t, service)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}
	reconcileTwice(t, ctx, reconciler, request)

	var pools inferencev1alpha1.ModelPoolList
	if err := kubeClient.List(ctx, &pools, client.InNamespace(service.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(pools.Items) != 0 {
		t.Fatalf("invalid intent created ModelPools: %#v", pools.Items)
	}
	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err != nil {
		t.Fatal(err)
	}
	if condition := meta.FindStatusCondition(current.Status.Conditions, conditionIntentCompiled); condition == nil || condition.Status != metav1.ConditionFalse {
		t.Fatalf("IntentCompiled condition = %#v", condition)
	}
}

func TestModelServiceDoesNotAdoptConflictingPool(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	controllerutil.AddFinalizer(service, modelServiceFinalizer)
	conflict := &inferencev1alpha1.ModelPool{ObjectMeta: metav1.ObjectMeta{Name: "demo-default", Namespace: service.Namespace}}
	reconciler, _ := testReconciler(t, service, conflict)

	if _, err := reconciler.Reconcile(ctx, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}); err == nil {
		t.Fatal("conflicting ModelPool was adopted or overwritten")
	}
}

func TestModelServiceDeleteRemovesOwnedPoolsBeforeFinalizer(t *testing.T) {
	ctx := context.Background()
	service := testModelService("demo", 3)
	now := metav1.Now()
	service.DeletionTimestamp = &now
	controllerutil.AddFinalizer(service, modelServiceFinalizer)
	pool := &inferencev1alpha1.ModelPool{
		ObjectMeta: metav1.ObjectMeta{
			Name:      "demo-default",
			Namespace: service.Namespace,
			OwnerReferences: []metav1.OwnerReference{{
				APIVersion: service.APIVersion,
				Kind:       service.Kind,
				Name:       service.Name,
				UID:        service.UID,
				Controller: ptr(true),
			}},
		},
		Spec: inferencev1alpha1.ModelPoolSpec{
			ModelServiceRef: inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)},
			PoolName:        "default",
		},
	}
	reconciler, kubeClient := testReconciler(t, service, pool)
	request := ctrl.Request{NamespacedName: client.ObjectKeyFromObject(service)}

	result, err := reconciler.Reconcile(ctx, request)
	if err != nil || !result.Requeue {
		t.Fatalf("deletion Reconcile() = (%#v, %v), want requeue", result, err)
	}
	var pools inferencev1alpha1.ModelPoolList
	if err := kubeClient.List(ctx, &pools, client.InNamespace(service.Namespace)); err != nil {
		t.Fatal(err)
	}
	if len(pools.Items) != 0 {
		t.Fatalf("ModelPools remain after deletion: %#v", pools.Items)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	current := new(inferencev1alpha1.ModelService)
	if err := kubeClient.Get(ctx, request.NamespacedName, current); err == nil && controllerutil.ContainsFinalizer(current, modelServiceFinalizer) {
		t.Fatal("ModelService finalizer remains after child deletion")
	}
}

type fakePoolMetricsProvider struct {
	observation algorithm.DemandObservation
	err         error
	targets     []algorithm.TargetID
}

func (provider *fakePoolMetricsProvider) Observation(_ context.Context, target algorithm.TargetID) (algorithm.DemandObservation, error) {
	provider.targets = append(provider.targets, target)
	return provider.observation, provider.err
}

func freshObservation(queue, active int64) algorithm.DemandObservation {
	now := time.Now()
	return algorithm.DemandObservation{
		State:          algorithm.ObservationFresh,
		Window:         algorithm.ObservationWindow{Start: now.Add(-time.Second), End: now, CollectedAt: now, Samples: 1, Complete: true},
		QueueRequests:  queue,
		ActiveRequests: active,
	}
}

func TestDemandObservationUsesCollectionCompletionForFreshness(t *testing.T) {
	evaluatedAt := time.Now()
	observedAt := evaluatedAt.Add(time.Second)
	provider := &fakePoolMetricsProvider{observation: algorithm.DemandObservation{
		State: algorithm.ObservationFresh,
		Window: algorithm.ObservationWindow{
			Start:       evaluatedAt,
			End:         observedAt,
			CollectedAt: observedAt.Add(time.Second),
			Samples:     1,
			Complete:    true,
		},
	}}
	reconciler := &ModelServiceReconciler{PoolMetricsProvider: provider}
	observation := reconciler.demandObservation(
		context.Background(),
		algorithm.TargetID{Name: "default"},
		evaluatedAt,
		15*time.Second,
	)
	if observation.State != algorithm.ObservationFresh {
		t.Fatalf("observation collected after evaluation = %#v", observation)
	}

	provider.observation.Window.End = provider.observation.Window.CollectedAt.Add(time.Second)
	observation = reconciler.demandObservation(
		context.Background(),
		algorithm.TargetID{Name: "default"},
		evaluatedAt,
		15*time.Second,
	)
	if observation.State != algorithm.ObservationStale {
		t.Fatalf("future source timestamp = %#v", observation)
	}
}

func TestScalingConfigUsesTypedQueuePolicy(t *testing.T) {
	service := testModelService("queue-autoscaling", 1)
	minimum, maximum, upStep, downStep := int32(1), int32(4), int32(2), int32(1)
	target := int64(3)
	service.Spec.Autoscaling = &inferencev1alpha1.ModelAutoscalingConfig{
		Algorithm:                   inferencev1alpha1.AutoscalingAlgorithmQueue,
		MinGroups:                   &minimum,
		MaxGroups:                   &maximum,
		TargetQueuePerRoutableGroup: &target,
		PollInterval:                "7s",
		MetricsMaxAge:               "21s",
		MaxScaleUpStep:              &upStep,
		MaxScaleDownStep:            &downStep,
	}
	config, err := (&ModelServiceReconciler{}).scalingConfig(service)
	if err != nil {
		t.Fatal(err)
	}
	if !config.Automatic || config.Limits.MaxGroups != 4 || config.Limits.MaxScaleUpStep != 2 || config.Limits.MaxScaleDownStep != 1 || config.PollInterval != 7*time.Second || config.MetricsMaxAge != 21*time.Second {
		t.Fatalf("scaling config = %#v", config)
	}
}

func TestPoolMetricsUnavailableHoldsCurrentCapacity(t *testing.T) {
	service := testModelService("metrics-unavailable", 1)
	pool := readyPool(service, "default", inferencev1alpha1.ModelRoleAggregate, 1, "r1")
	group := readyGroup(pool, inferencev1alpha1.ModelRoleAggregate, "r1", 0)
	reconciler, _ := testReconciler(t, service, pool, group)
	planner, err := autoscaling.New(autoscaling.Configuration{Algorithm: autoscaling.AlgorithmQueue, TargetQueuePerRoutableGroup: 1})
	if err != nil {
		t.Fatal(err)
	}
	provider := &fakePoolMetricsProvider{observation: algorithm.DemandObservation{State: algorithm.ObservationUnavailable}}
	reconciler.Autoscaler, reconciler.PoolMetricsProvider = planner, provider
	compiled, err := compiler.CompileModelService(service.Spec)
	if err != nil {
		t.Fatal(err)
	}
	resolved, statuses, err := reconciler.applyScaling(context.Background(), service, compiled)
	if err != nil {
		t.Fatal(err)
	}
	if resolved[0].DesiredGroups != 1 || statuses[0].ObservationState != string(algorithm.ObservationUnavailable) || statuses[0].AppliedGroups != 1 {
		t.Fatalf("unavailable scaling = resolved=%#v statuses=%#v", resolved, statuses)
	}
	if len(provider.targets) != 1 || provider.targets[0].Name != "default" {
		t.Fatalf("provider targets = %#v", provider.targets)
	}
}

func TestPoolMetricsFreshQueueScalesFromZero(t *testing.T) {
	service := testModelService("metrics-zero", 1)
	zero, maximum := int32(0), int32(2)
	service.Spec.Replicas = &zero
	service.Spec.Autoscaling = &inferencev1alpha1.ModelAutoscalingConfig{
		Algorithm:                   inferencev1alpha1.AutoscalingAlgorithmQueue,
		MinGroups:                   &zero,
		MaxGroups:                   &maximum,
		TargetQueuePerRoutableGroup: ptr(int64(0)),
	}
	pool := readyPool(service, "default", inferencev1alpha1.ModelRoleAggregate, 0, "r1")
	reconciler, _ := testReconciler(t, service, pool)
	reconciler.PoolMetricsProvider = &fakePoolMetricsProvider{observation: freshObservation(1, 0)}
	compiled, err := compiler.CompileModelService(service.Spec)
	if err != nil {
		t.Fatal(err)
	}
	resolved, statuses, err := reconciler.applyScaling(context.Background(), service, compiled)
	if err != nil {
		t.Fatal(err)
	}
	if resolved[0].DesiredGroups != 1 || statuses[0].AppliedGroups != 1 || statuses[0].Direction != string(algorithm.DirectionUp) {
		t.Fatalf("scale from zero = resolved=%#v statuses=%#v", resolved, statuses)
	}
}

func TestPoolMetricsFreshQueueScalesReadyCapacityUp(t *testing.T) {
	service := testModelService("metrics-up", 1)
	pool := readyPool(service, "default", inferencev1alpha1.ModelRoleAggregate, 1, "r1")
	group := readyGroup(pool, inferencev1alpha1.ModelRoleAggregate, "r1", 0)
	reconciler, _ := testReconciler(t, service, pool, group)
	planner, err := autoscaling.New(autoscaling.Configuration{Algorithm: autoscaling.AlgorithmQueue, TargetQueuePerRoutableGroup: 0})
	if err != nil {
		t.Fatal(err)
	}
	reconciler.Autoscaler = planner
	reconciler.PoolMetricsProvider = &fakePoolMetricsProvider{observation: freshObservation(1, 0)}
	compiled, _ := compiler.CompileModelService(service.Spec)
	resolved, _, err := reconciler.applyScaling(context.Background(), service, compiled)
	if err != nil {
		t.Fatal(err)
	}
	if resolved[0].DesiredGroups != 2 {
		t.Fatalf("desiredGroups = %d, want 2", resolved[0].DesiredGroups)
	}
}

func TestPoolMetricsFreshIdleScalesReadyCapacityDown(t *testing.T) {
	service := testModelService("metrics-down", 1)
	two := int32(2)
	service.Spec.Replicas = &two
	pool := readyPool(service, "default", inferencev1alpha1.ModelRoleAggregate, 2, "r1")
	groups := []client.Object{service, pool, readyGroup(pool, inferencev1alpha1.ModelRoleAggregate, "r1", 0), readyGroup(pool, inferencev1alpha1.ModelRoleAggregate, "r1", 1)}
	reconciler, _ := testReconciler(t, groups...)
	planner, err := autoscaling.New(autoscaling.Configuration{Algorithm: autoscaling.AlgorithmQueue, TargetQueuePerRoutableGroup: 1})
	if err != nil {
		t.Fatal(err)
	}
	reconciler.Autoscaler = planner
	reconciler.PoolMetricsProvider = &fakePoolMetricsProvider{observation: freshObservation(0, 0)}
	compiled, _ := compiler.CompileModelService(service.Spec)
	resolved, _, err := reconciler.applyScaling(context.Background(), service, compiled)
	if err != nil {
		t.Fatal(err)
	}
	if resolved[0].DesiredGroups != 1 {
		t.Fatalf("desiredGroups = %d, want 1", resolved[0].DesiredGroups)
	}
}

func TestCapacityUsesActiveRevisionForRoutableGroups(t *testing.T) {
	service := testModelService("capacity", 1)
	pool := readyPool(service, "default", inferencev1alpha1.ModelRoleAggregate, 2, "active")
	active := readyGroup(pool, inferencev1alpha1.ModelRoleAggregate, "active", 0)
	old := readyGroup(pool, inferencev1alpha1.ModelRoleAggregate, "old", 1)
	capacity := modelPoolCapacity(pool, []inferencev1alpha1.ModelGroup{*active, *old})
	capacity.RequestedGroups = 2
	finalizeCapacity(&capacity)
	if capacity.ReadyGroups != 2 || capacity.RoutableGroups != 1 {
		t.Fatalf("capacity = %#v, want two ready and one active-revision routable", capacity)
	}
}

func TestEPDCapacityRequiresCompleteReadyOrdinalTriplets(t *testing.T) {
	service := testModelService("epd-capacity", 1)
	encoder := readyPool(service, "encoder", inferencev1alpha1.ModelRoleEncoder, 2, "r1")
	prefill := readyPool(service, "prefill", inferencev1alpha1.ModelRolePrefill, 2, "r1")
	decode := readyPool(service, "decode", inferencev1alpha1.ModelRoleDecode, 2, "r1")
	groups := []inferencev1alpha1.ModelGroup{
		*readyGroup(encoder, inferencev1alpha1.ModelRoleEncoder, "r1", 0),
		*readyGroup(prefill, inferencev1alpha1.ModelRolePrefill, "r1", 0),
		*readyGroup(decode, inferencev1alpha1.ModelRoleDecode, "r1", 0),
		*readyGroup(encoder, inferencev1alpha1.ModelRoleEncoder, "r1", 1),
		*readyGroup(prefill, inferencev1alpha1.ModelRolePrefill, "r1", 1),
	}
	capacity := epdDomainCapacity(map[string]*inferencev1alpha1.ModelPool{"encoder": encoder, "prefill": prefill, "decode": decode}, groups, 2)
	if capacity.ReadyGroups != 1 || capacity.RoutableGroups != 1 || capacity.PendingGroups != 1 {
		t.Fatalf("E/P/D capacity = %#v, want one complete and one pending ordinal", capacity)
	}
}

func readyPool(service *inferencev1alpha1.ModelService, name string, role inferencev1alpha1.ModelRole, desired int32, revision string) *inferencev1alpha1.ModelPool {
	pool := &inferencev1alpha1.ModelPool{ObjectMeta: metav1.ObjectMeta{Name: service.Name + "-" + name, Namespace: service.Namespace, UID: types.UID(service.Name + "-" + name), Generation: 1, OwnerReferences: []metav1.OwnerReference{{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelService", Name: service.Name, UID: service.UID, Controller: ptr(true)}}}, Spec: inferencev1alpha1.ModelPoolSpec{ModelServiceRef: inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)}, PoolName: name, DesiredGroups: desired, Template: inferencev1alpha1.NormalizedPoolTemplate{Role: role}}, Status: inferencev1alpha1.ModelPoolStatus{ObservedGeneration: 1, ActiveRevision: revision}}
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionReady, Status: metav1.ConditionTrue, ObservedGeneration: 1})
	return pool
}

func readyGroup(pool *inferencev1alpha1.ModelPool, role inferencev1alpha1.ModelRole, revision string, ordinal int32) *inferencev1alpha1.ModelGroup {
	group := &inferencev1alpha1.ModelGroup{ObjectMeta: metav1.ObjectMeta{Name: pool.Name + "-" + revision + "-" + string(rune('0'+ordinal)), Namespace: pool.Namespace, UID: types.UID(pool.Name + revision + string(rune('0'+ordinal))), Generation: 1, OwnerReferences: []metav1.OwnerReference{{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelPool", Name: pool.Name, UID: pool.UID, Controller: ptr(true)}}}, Spec: inferencev1alpha1.ModelGroupSpec{ModelPoolRef: inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)}, Role: role, Revision: revision, Ordinal: ordinal, MemberCount: 1}, Status: inferencev1alpha1.ModelGroupStatus{Phase: inferencev1alpha1.ModelGroupPhaseReady, ReadyMembers: 1}}
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionReady, Status: metav1.ConditionTrue, ObservedGeneration: 1})
	return group
}

func testReconciler(t *testing.T, objects ...client.Object) (*ModelServiceReconciler, client.Client) {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := inferencev1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	kubeClient := fake.NewClientBuilder().
		WithScheme(scheme).
		WithStatusSubresource(&inferencev1alpha1.ModelService{}, &inferencev1alpha1.ModelPool{}).
		WithObjects(objects...).
		Build()
	return &ModelServiceReconciler{Client: kubeClient}, kubeClient
}

func testModelService(name string, generation int64) *inferencev1alpha1.ModelService {
	return &inferencev1alpha1.ModelService{
		TypeMeta:   metav1.TypeMeta{APIVersion: inferencev1alpha1.GroupVersion.String(), Kind: "ModelService"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "default", UID: types.UID(name + "-uid"), Generation: generation},
		Spec: inferencev1alpha1.ModelServiceSpec{
			Model:   "Qwen/Qwen3-0.6B",
			Backend: "vllm",
			Resources: ptr(inferencev1alpha1.ModelResources{Requests: inferencev1alpha1.ModelResourceRequests{
				ComputeResourceRequests: inferencev1alpha1.ComputeResourceRequests{CPU: "1", Memory: "1Gi"},
				GPU:                     inferencev1alpha1.GPURequest{Type: "auto", Count: 1},
			}}),
			Timeouts:    inferencev1alpha1.ModelTimeouts{Startup: "10m", Drain: "2m"},
			Parallelism: &inferencev1alpha1.Parallelism{TP: 1, PP: 1, PCP: 1, DCP: 1},
		},
	}
}

func reconcileTwice(t *testing.T, ctx context.Context, reconciler *ModelServiceReconciler, request ctrl.Request) {
	t.Helper()
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
	if _, err := reconciler.Reconcile(ctx, request); err != nil {
		t.Fatal(err)
	}
}

func ptr[T any](value T) *T { return &value }
