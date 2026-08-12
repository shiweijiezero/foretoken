// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles ModelService intent into controller-owned ModelPool resources.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/algorithm"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	modelServiceFinalizer      = "inference.foretoken.io/modelservice-protection"
	conditionIntentCompiled    = "IntentCompiled"
	conditionPoolsMaterialized = "PoolsMaterialized"
	conditionReady             = "Ready"
	maxDesiredGroups           = int32(1<<31 - 1)
	defaultScalingPollInterval = 5 * time.Second
	defaultMetricsMaxAge       = 15 * time.Second
)

var defaultAutoscaler = autoscaling.Manual()

// PoolMetricsProvider supplies one read-only, target-attributed demand observation.
// Implementations must not modify Kubernetes resources or autoscaling algorithms.
type PoolMetricsProvider interface {
	Observation(context.Context, algorithm.TargetID) (algorithm.DemandObservation, error)
}

// ModelServiceReconciler compiles ModelService intent and owns ModelPool specs.
type ModelServiceReconciler struct {
	client.Client
	Autoscaler          *autoscaling.Autoscaler
	PoolMetricsProvider PoolMetricsProvider
}

type modelScalingConfig struct {
	Autoscaler    *autoscaling.Autoscaler
	Limits        algorithm.CapacityLimits
	PollInterval  time.Duration
	MetricsMaxAge time.Duration
	Automatic     bool
}

// SetupWithManager registers the ModelService controller and its owned resources.
func (reconciler *ModelServiceReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.ModelService{}).
		Owns(&inferencev1alpha1.ModelPool{}).
		Watches(&inferencev1alpha1.KVService{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesForKVService)).
		Complete(reconciler)
}

func (reconciler *ModelServiceReconciler) scalingConfig(service *inferencev1alpha1.ModelService) (modelScalingConfig, error) {
	config := modelScalingConfig{
		Autoscaler:    defaultAutoscaler,
		Limits:        algorithm.CapacityLimits{MinGroups: 0, MaxGroups: maxDesiredGroups},
		PollInterval:  defaultScalingPollInterval,
		MetricsMaxAge: defaultMetricsMaxAge,
	}
	if reconciler.Autoscaler != nil {
		config.Autoscaler = reconciler.Autoscaler
	}
	autoscalingConfig := service.Spec.Autoscaling
	if autoscalingConfig == nil {
		return config, nil
	}
	if autoscalingConfig.MinGroups != nil {
		config.Limits.MinGroups = *autoscalingConfig.MinGroups
	}
	if autoscalingConfig.MaxGroups != nil {
		config.Limits.MaxGroups = *autoscalingConfig.MaxGroups
	}
	config.Limits.MaxScaleUpStep = int32OrDefault(autoscalingConfig.MaxScaleUpStep, 1)
	config.Limits.MaxScaleDownStep = int32OrDefault(autoscalingConfig.MaxScaleDownStep, 1)
	pollInterval, err := durationOrDefault(autoscalingConfig.PollInterval, defaultScalingPollInterval)
	if err != nil {
		return modelScalingConfig{}, fmt.Errorf("autoscaling pollInterval: %w", err)
	}
	metricsMaxAge, err := durationOrDefault(autoscalingConfig.MetricsMaxAge, defaultMetricsMaxAge)
	if err != nil {
		return modelScalingConfig{}, fmt.Errorf("autoscaling metricsMaxAge: %w", err)
	}
	config.PollInterval = pollInterval
	config.MetricsMaxAge = metricsMaxAge
	name := autoscaling.AlgorithmName(autoscalingConfig.Algorithm)
	if name == "" {
		name = autoscaling.AlgorithmManual
	}
	config.Automatic = name == autoscaling.AlgorithmQueue
	if reconciler.Autoscaler == nil {
		selected, err := autoscaling.New(autoscaling.Configuration{Algorithm: name, TargetQueuePerRoutableGroup: int64OrDefault(autoscalingConfig.TargetQueuePerRoutableGroup, 1)})
		if err != nil {
			return modelScalingConfig{}, err
		}
		config.Autoscaler = selected
	}
	return config, nil
}

func int32OrDefault(value *int32, fallback int32) int32 {
	if value == nil {
		return fallback
	}
	return *value
}

func int64OrDefault(value *int64, fallback int64) int64 {
	if value == nil {
		return fallback
	}
	return *value
}

func durationOrDefault(value inferencev1alpha1.Duration, fallback time.Duration) (time.Duration, error) {
	if value == "" {
		return fallback, nil
	}
	duration, err := time.ParseDuration(string(value))
	if err != nil || duration <= 0 {
		return 0, fmt.Errorf("must be a positive duration")
	}
	return duration, nil
}

// Reconcile materializes stable ModelPools and aggregates their serving readiness.
func (reconciler *ModelServiceReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	service := new(inferencev1alpha1.ModelService)
	if err := reconciler.Get(ctx, request.NamespacedName, service); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	if !service.DeletionTimestamp.IsZero() {
		return reconciler.reconcileDelete(ctx, service)
	}
	if !controllerutil.ContainsFinalizer(service, modelServiceFinalizer) {
		base := service.DeepCopy()
		controllerutil.AddFinalizer(service, modelServiceFinalizer)
		if err := reconciler.Patch(ctx, service, client.MergeFrom(base)); err != nil {
			return ctrl.Result{}, fmt.Errorf("add ModelService finalizer: %w", err)
		}
		return ctrl.Result{Requeue: true}, nil
	}

	compiledPools, err := compiler.CompileModelService(service.Spec)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionFalse, "InvalidIntent", err.Error()},
			pools:    conditionState{metav1.ConditionFalse, "CompilationFailed", "ModelService intent was not compiled"},
			ready:    conditionState{metav1.ConditionFalse, "InvalidIntent", "ModelService intent is invalid"},
		})
	}
	if err := reconciler.resolveManagedKVBindings(ctx, service, compiledPools); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionFalse, "KVServiceNotReady", err.Error()},
			pools:    conditionState{metav1.ConditionFalse, "ResolutionFailed", "No new ModelPools were materialized"},
			ready:    conditionState{metav1.ConditionFalse, "KVServiceNotReady", "Referenced KVService is not ready"},
		})
	}
	compiledPools, autoscalingStatus, err := reconciler.applyScaling(ctx, service, compiledPools)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionFalse, "ScalingFailed", err.Error()},
			pools:    conditionState{metav1.ConditionFalse, "ScalingFailed", "ModelPool capacity was not resolved"},
			ready:    conditionState{metav1.ConditionFalse, "ScalingFailed", "ModelService capacity is invalid"},
		})
	}

	if err := reconciler.reconcilePools(ctx, service, compiledPools); err != nil {
		statusErr := reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionTrue, "Compiled", "ModelService intent was compiled"},
			pools:    conditionState{metav1.ConditionFalse, "ApplyFailed", "ModelPools were not fully materialized"},
			ready:    conditionState{metav1.ConditionFalse, "PoolsNotReady", "ModelPools are not ready"},
		})
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	ready, readyReason, readyMessage, err := reconciler.serviceReadiness(ctx, service, compiledPools)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.updateStatus(ctx, service, modelServiceState{
		compiled:    conditionState{metav1.ConditionTrue, "Compiled", "ModelService intent was compiled"},
		pools:       conditionState{metav1.ConditionTrue, "Applied", "All ModelPools were materialized"},
		ready:       conditionState{conditionStatus(ready), readyReason, readyMessage},
		autoscaling: &autoscalingStatus,
	}); err != nil {
		return ctrl.Result{}, err
	}
	scaling, err := reconciler.scalingConfig(service)
	if err != nil {
		return ctrl.Result{}, err
	}
	if scaling.Automatic {
		return ctrl.Result{RequeueAfter: scaling.PollInterval}, nil
	}
	return ctrl.Result{}, nil
}

func (reconciler *ModelServiceReconciler) applyScaling(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) ([]compiler.ModelPool, []inferencev1alpha1.AutoscalingTargetStatus, error) {
	owned, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return nil, nil, err
	}
	byPoolName := make(map[string]*inferencev1alpha1.ModelPool, len(owned))
	for index := range owned {
		pool := &owned[index]
		byPoolName[pool.Spec.PoolName] = pool
	}
	var groupList inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groupList, client.InNamespace(service.Namespace)); err != nil {
		return nil, nil, fmt.Errorf("list ModelGroups: %w", err)
	}
	scaling, err := reconciler.scalingConfig(service)
	if err != nil {
		return nil, nil, err
	}

	evaluatedAt := metav1.Now()
	policyRevision := fmt.Sprintf("%d", service.Generation)
	snapshots := make([]algorithm.Snapshot, 0, len(compiledPools))
	epdIndexes := make([]int, 0, 3)
	hasEPD := false
	for index, compiled := range compiledPools {
		if compiled.Template.Role == inferencev1alpha1.ModelRoleEncoder {
			hasEPD = true
		}
		if isEPDRole(compiled.Template.Role) {
			epdIndexes = append(epdIndexes, index)
		}
	}
	for _, compiled := range compiledPools {
		if hasEPD && isEPDRole(compiled.Template.Role) {
			continue
		}
		pool := byPoolName[compiled.Name]
		current := compiled.DesiredGroups
		transitioning := false
		poolUID := ""
		if pool != nil {
			current = pool.Spec.DesiredGroups
			transitioning = modelPoolTransitioning(pool)
			poolUID = string(pool.UID)
		}
		target := algorithm.TargetID{
			ServiceNamespace: service.Namespace,
			ServiceName:      service.Name,
			ServiceUID:       string(service.UID),
			Name:             compiled.Name,
			UID:              poolUID,
			Kind:             algorithm.TargetPool,
			Role:             autoscalingRole(compiled.Template.Role),
		}
		capacity := modelPoolCapacity(pool, groupList.Items)
		capacity.BaselineGroups = compiled.DesiredGroups
		capacity.RequestedGroups = current
		capacity.Transitioning = capacity.Transitioning || transitioning
		finalizeCapacity(&capacity)
		// A Pool not yet created has no transition to fence; its first controller
		// write remains eligible for the selected algorithm's bootstrap decision.
		if pool == nil {
			capacity.Transitioning = false
		}
		snapshots = append(snapshots, reconciler.scalingSnapshot(ctx, service, target, policyRevision, evaluatedAt, capacity, scaling))
	}
	if hasEPD {
		snapshot, err := reconciler.epdScalingSnapshot(ctx, service, compiledPools, epdIndexes, byPoolName, groupList.Items, policyRevision, evaluatedAt, scaling)
		if err != nil {
			return nil, nil, err
		}
		snapshots = append(snapshots, snapshot)
	}
	decisions, err := scaling.Autoscaler.Plan(ctx, snapshots)
	if err != nil {
		return nil, nil, err
	}
	if len(decisions) != len(snapshots) {
		return nil, nil, fmt.Errorf("autoscaler returned %d decisions for %d scaling targets", len(decisions), len(snapshots))
	}

	bySnapshotTarget := make(map[algorithm.TargetID]algorithm.Snapshot, len(snapshots))
	for _, snapshot := range snapshots {
		bySnapshotTarget[snapshot.Target] = snapshot
	}
	byTarget := make(map[algorithm.TargetID]int32, len(decisions))
	statuses := make([]inferencev1alpha1.AutoscalingTargetStatus, 0, len(decisions))
	for _, decision := range decisions {
		if _, exists := byTarget[decision.Target]; exists {
			return nil, nil, fmt.Errorf("autoscaler returned duplicate decision for target %q", decision.Target.Name)
		}
		snapshot, exists := bySnapshotTarget[decision.Target]
		if !exists {
			return nil, nil, fmt.Errorf("autoscaler returned unknown target %q", decision.Target.Name)
		}
		byTarget[decision.Target] = decision.AppliedGroups
		reason := decision.Recommendation.Reason
		if decision.Constraint != "" {
			reason = decision.Constraint
		}
		statuses = append(statuses, inferencev1alpha1.AutoscalingTargetStatus{
			ID:               fmt.Sprintf("%s/%s", decision.Target.Kind, decision.Target.Name),
			Kind:             string(decision.Target.Kind),
			Role:             string(decision.Target.Role),
			Algorithm:        decision.Algorithm,
			SnapshotID:       decision.Recommendation.SnapshotID,
			ObservedAt:       metav1.NewTime(snapshot.EvaluatedAt),
			ObservationState: string(snapshot.Observation.State),
			Disposition:      string(decision.Recommendation.Disposition),
			Reason:           string(reason),
			Message:          decision.Message,
			Direction:        string(decision.Direction),
			RequestedGroups:  decision.Recommendation.DesiredGroups,
			AppliedGroups:    decision.AppliedGroups,
			ReadyGroups:      snapshot.Capacity.ReadyGroups,
			RoutableGroups:   snapshot.Capacity.RoutableGroups,
		})
	}
	resolved := append([]compiler.ModelPool(nil), compiledPools...)
	for index := range resolved {
		compiled := resolved[index]
		var target algorithm.TargetID
		if hasEPD && isEPDRole(compiled.Template.Role) {
			target = epdTargetID(service)
		} else {
			poolUID := ""
			if pool := byPoolName[compiled.Name]; pool != nil {
				poolUID = string(pool.UID)
			}
			target = algorithm.TargetID{ServiceNamespace: service.Namespace, ServiceName: service.Name, ServiceUID: string(service.UID), Name: compiled.Name, UID: poolUID, Kind: algorithm.TargetPool, Role: autoscalingRole(compiled.Template.Role)}
		}
		desired, exists := byTarget[target]
		if !exists {
			return nil, nil, fmt.Errorf("autoscaler omitted target %q", target.Name)
		}
		resolved[index].DesiredGroups = desired
	}
	return resolved, statuses, nil
}

func (reconciler *ModelServiceReconciler) scalingSnapshot(ctx context.Context, service *inferencev1alpha1.ModelService, target algorithm.TargetID, policyRevision string, evaluatedAt metav1.Time, capacity algorithm.CapacityState, scaling modelScalingConfig) algorithm.Snapshot {
	return algorithm.Snapshot{
		Target:      target,
		Ref:         algorithm.SnapshotRef{ID: fmt.Sprintf("%s/%s/%s/%s", service.UID, service.ResourceVersion, target.Kind, target.Name), PolicyRevision: policyRevision},
		EvaluatedAt: evaluatedAt.Time,
		Capacity:    capacity,
		Limits:      scaling.Limits,
		Observation: reconciler.demandObservation(ctx, target, evaluatedAt.Time, scaling.MetricsMaxAge),
	}
}

// demandObservation fails closed: a missing or failed provider can never be interpreted as zero demand.
func (reconciler *ModelServiceReconciler) demandObservation(ctx context.Context, target algorithm.TargetID, _ time.Time, maxAge time.Duration) algorithm.DemandObservation {
	if reconciler.PoolMetricsProvider == nil {
		return algorithm.DemandObservation{State: algorithm.ObservationUnavailable}
	}
	observation, err := reconciler.PoolMetricsProvider.Observation(ctx, target)
	if err != nil || observation.State == "" {
		return algorithm.DemandObservation{State: algorithm.ObservationUnavailable}
	}
	if observation.State == algorithm.ObservationFresh {
		if observation.Window.End.IsZero() || observation.Window.CollectedAt.IsZero() {
			return algorithm.DemandObservation{State: algorithm.ObservationUnavailable}
		}
		age := observation.Window.CollectedAt.Sub(observation.Window.End)
		if age < 0 || age > maxAge {
			observation.State = algorithm.ObservationStale
		}
	}
	return observation
}

func (reconciler *ModelServiceReconciler) epdScalingSnapshot(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool, indexes []int, owned map[string]*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, policyRevision string, evaluatedAt metav1.Time, scaling modelScalingConfig) (algorithm.Snapshot, error) {
	if len(indexes) != 3 {
		return algorithm.Snapshot{}, fmt.Errorf("E/P/D scaling requires exactly one encoder, prefill, and decode Pool")
	}
	seenRoles := make(map[inferencev1alpha1.ModelRole]struct{}, 3)
	baseline := pools[indexes[0]].DesiredGroups
	requested := int32(0)
	hasRequested := false
	transitioning := false
	for _, index := range indexes {
		pool := pools[index]
		if _, exists := seenRoles[pool.Template.Role]; exists {
			return algorithm.Snapshot{}, fmt.Errorf("E/P/D scaling requires exactly one %s Pool", pool.Template.Role)
		}
		seenRoles[pool.Template.Role] = struct{}{}
		if pool.DesiredGroups != baseline {
			return algorithm.Snapshot{}, fmt.Errorf("E/P/D scaling requires equal baseline capacity")
		}
		if existing := owned[pool.Name]; existing != nil {
			// A failed multi-object write can temporarily leave E/P/D Pools at
			// different desired counts. Use the highest request as the safe
			// recovery baseline so scale-down never removes a partial triplet.
			if !hasRequested || existing.Spec.DesiredGroups > requested {
				requested = existing.Spec.DesiredGroups
			}
			hasRequested = true
			transitioning = transitioning || modelPoolTransitioning(existing)
		} else {
			transitioning = true
		}
	}
	if !hasRequested {
		requested = baseline
	}
	for _, role := range []inferencev1alpha1.ModelRole{inferencev1alpha1.ModelRoleEncoder, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode} {
		if _, exists := seenRoles[role]; !exists {
			return algorithm.Snapshot{}, fmt.Errorf("E/P/D scaling requires a %s Pool", role)
		}
	}
	capacity := epdDomainCapacity(owned, groups, requested)
	capacity.BaselineGroups = baseline
	capacity.RequestedGroups = requested
	capacity.Transitioning = capacity.Transitioning || transitioning
	finalizeCapacity(&capacity)
	return reconciler.scalingSnapshot(ctx, service, epdTargetID(service), policyRevision, evaluatedAt, capacity, scaling), nil
}

// epdScalingSnapshot preserves the pure snapshot helper used by controller tests.
func epdScalingSnapshot(service *inferencev1alpha1.ModelService, pools []compiler.ModelPool, indexes []int, owned map[string]*inferencev1alpha1.ModelPool, policyRevision string, evaluatedAt metav1.Time) (algorithm.Snapshot, error) {
	scaling := modelScalingConfig{Autoscaler: defaultAutoscaler, Limits: algorithm.CapacityLimits{MinGroups: 0, MaxGroups: maxDesiredGroups}, MetricsMaxAge: defaultMetricsMaxAge}
	return (&ModelServiceReconciler{}).epdScalingSnapshot(context.Background(), service, pools, indexes, owned, nil, policyRevision, evaluatedAt, scaling)
}

func epdTargetID(service *inferencev1alpha1.ModelService) algorithm.TargetID {
	return algorithm.TargetID{ServiceNamespace: service.Namespace, ServiceName: service.Name, ServiceUID: string(service.UID), Name: "epd", Kind: algorithm.TargetEPDDomain, Role: algorithm.RoleEPD}
}

func autoscalingRole(role inferencev1alpha1.ModelRole) algorithm.TargetRole {
	switch role {
	case inferencev1alpha1.ModelRoleEncoder:
		return algorithm.RoleEncoder
	case inferencev1alpha1.ModelRolePrefill:
		return algorithm.RolePrefill
	case inferencev1alpha1.ModelRoleDecode:
		return algorithm.RoleDecode
	default:
		return algorithm.RoleAggregate
	}
}

func isEPDRole(role inferencev1alpha1.ModelRole) bool {
	switch role {
	case inferencev1alpha1.ModelRoleEncoder, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode:
		return true
	default:
		return false
	}
}

func (reconciler *ModelServiceReconciler) reconcilePools(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) error {
	owned, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return err
	}
	byPoolName := make(map[string]*inferencev1alpha1.ModelPool, len(owned))
	for index := range owned {
		pool := &owned[index]
		if previous := byPoolName[pool.Spec.PoolName]; previous != nil {
			return fmt.Errorf("ModelService owns duplicate ModelPools for poolName %q", pool.Spec.PoolName)
		}
		byPoolName[pool.Spec.PoolName] = pool
	}

	desired := make(map[string]struct{}, len(compiledPools))
	for _, compiled := range compiledPools {
		desired[compiled.Name] = struct{}{}
		pool := byPoolName[compiled.Name]
		if pool == nil {
			pool = &inferencev1alpha1.ModelPool{ObjectMeta: metav1.ObjectMeta{Namespace: service.Namespace}}
			name := service.Name + "-" + compiled.Name
			if len(name) <= 63 {
				pool.Name = name
			} else {
				prefix := service.Name
				if len(prefix) > 52 {
					prefix = prefix[:52]
				}
				pool.GenerateName = prefix + "-"
			}
		}

		created := pool.ResourceVersion == ""
		before := pool.DeepCopy()
		pool.Spec = inferencev1alpha1.ModelPoolSpec{
			ModelServiceRef: inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)},
			PoolName:        compiled.Name,
			DesiredGroups:   compiled.DesiredGroups,
			Template:        compiled.Template,
		}
		if err := controllerutil.SetControllerReference(service, pool, reconciler.Scheme()); err != nil {
			return fmt.Errorf("set ModelPool %q owner: %w", compiled.Name, err)
		}

		if created {
			if err := reconciler.Create(ctx, pool); err != nil {
				return fmt.Errorf("create ModelPool %q: %w", compiled.Name, err)
			}
		} else if !reflect.DeepEqual(before.Spec, pool.Spec) || !reflect.DeepEqual(before.OwnerReferences, pool.OwnerReferences) {
			if err := reconciler.Update(ctx, pool); err != nil {
				return fmt.Errorf("update ModelPool %q: %w", compiled.Name, err)
			}
		}
	}

	for index := range owned {
		pool := &owned[index]
		if _, keep := desired[pool.Spec.PoolName]; keep {
			continue
		}
		if err := reconciler.Delete(ctx, pool); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("delete stale ModelPool %q: %w", pool.Name, err)
		}
	}
	return nil
}

func (reconciler *ModelServiceReconciler) serviceReadiness(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) (bool, string, string, error) {
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return false, "PoolsNotReady", "ModelPools are not ready", err
	}
	if len(pools) != len(compiledPools) {
		return false, "PoolsNotReady", "ModelPool reconciliation has not converged", nil
	}
	byPoolName := make(map[string]*inferencev1alpha1.ModelPool, len(pools))
	for index := range pools {
		pool := &pools[index]
		byPoolName[pool.Spec.PoolName] = pool
	}
	hasCapacity := false
	for _, compiled := range compiledPools {
		if compiled.DesiredGroups == 0 {
			continue
		}
		hasCapacity = true
		pool := byPoolName[compiled.Name]
		if pool == nil || pool.Spec.DesiredGroups != compiled.DesiredGroups || !modelPoolReady(pool) {
			return false, "PoolsNotReady", "One or more ModelPools are not ready", nil
		}
	}
	if !hasCapacity {
		return false, "ScaledToZero", "ModelService has no requested serving capacity", nil
	}
	return true, "Ready", "All serving ModelPools are ready", nil
}

func modelPoolReady(pool *inferencev1alpha1.ModelPool) bool {
	if pool == nil || !pool.DeletionTimestamp.IsZero() || pool.Status.ObservedGeneration != pool.Generation {
		return false
	}
	condition := meta.FindStatusCondition(pool.Status.Conditions, conditionReady)
	return condition != nil &&
		condition.Status == metav1.ConditionTrue &&
		condition.ObservedGeneration == pool.Generation
}

func modelPoolTransitioning(pool *inferencev1alpha1.ModelPool) bool {
	if !modelPoolReady(pool) {
		return true
	}
	condition := meta.FindStatusCondition(pool.Status.Conditions, conditionRolloutPending)
	return condition != nil && condition.Status == metav1.ConditionTrue && condition.ObservedGeneration == pool.Generation
}

func (reconciler *ModelServiceReconciler) reconcileDelete(ctx context.Context, service *inferencev1alpha1.ModelService) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(service, modelServiceFinalizer) {
		return ctrl.Result{}, nil
	}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	if len(pools) > 0 {
		for index := range pools {
			if err := reconciler.Delete(ctx, &pools[index]); err != nil && !apierrors.IsNotFound(err) {
				return ctrl.Result{}, fmt.Errorf("delete ModelPool %q: %w", pools[index].Name, err)
			}
		}
		return ctrl.Result{Requeue: true}, nil
	}

	base := service.DeepCopy()
	controllerutil.RemoveFinalizer(service, modelServiceFinalizer)
	if err := reconciler.Patch(ctx, service, client.MergeFrom(base)); err != nil {
		return ctrl.Result{}, fmt.Errorf("remove ModelService finalizer: %w", err)
	}
	return ctrl.Result{}, nil
}

func (reconciler *ModelServiceReconciler) ownedPools(ctx context.Context, service *inferencev1alpha1.ModelService) ([]inferencev1alpha1.ModelPool, error) {
	var list inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &list, client.InNamespace(service.Namespace)); err != nil {
		return nil, fmt.Errorf("list ModelPools: %w", err)
	}
	owned := make([]inferencev1alpha1.ModelPool, 0, len(list.Items))
	for index := range list.Items {
		pool := &list.Items[index]
		referenceMatches := pool.Spec.ModelServiceRef.Name == service.Name && pool.Spec.ModelServiceRef.UID == string(service.UID)
		ownerMatches := metav1.IsControlledBy(pool, service)
		if referenceMatches != ownerMatches {
			return nil, fmt.Errorf("ModelPool %q has inconsistent ModelService ownership", pool.Name)
		}
		if referenceMatches {
			owned = append(owned, *pool)
		}
	}
	return owned, nil
}

type conditionState struct {
	Status  metav1.ConditionStatus
	Reason  string
	Message string
}

type modelServiceState struct {
	compiled    conditionState
	pools       conditionState
	ready       conditionState
	autoscaling *[]inferencev1alpha1.AutoscalingTargetStatus
}

func (reconciler *ModelServiceReconciler) updateStatus(ctx context.Context, service *inferencev1alpha1.ModelService, state modelServiceState) error {
	base := service.DeepCopy()
	service.Status.ObservedGeneration = service.Generation
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionIntentCompiled, Status: state.compiled.Status, Reason: state.compiled.Reason, Message: state.compiled.Message, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionPoolsMaterialized, Status: state.pools.Status, Reason: state.pools.Reason, Message: state.pools.Message, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionReady, Status: state.ready.Status, Reason: state.ready.Reason, Message: state.ready.Message, ObservedGeneration: service.Generation})
	if state.autoscaling != nil {
		service.Status.Autoscaling = append([]inferencev1alpha1.AutoscalingTargetStatus(nil), (*state.autoscaling)...)
	}
	if reflect.DeepEqual(base.Status, service.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, service, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelService status: %w", err)
	}
	return nil
}

// resolveManagedKVBindings replaces a user name reference with the KVService UID and
// current binding contract before any ModelPool is created or changed.
func (reconciler *ModelServiceReconciler) resolveManagedKVBindings(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool) error {
	for i := range pools {
		store := modelServicePoolStore(service, pools[i].Name)
		if store == nil || store.KVServiceRef == nil {
			continue
		}
		kv := new(inferencev1alpha1.KVService)
		if err := reconciler.Get(ctx, client.ObjectKey{Namespace: service.Namespace, Name: store.KVServiceRef.Name}, kv); err != nil {
			return fmt.Errorf("get KVService %q: %w", store.KVServiceRef.Name, err)
		}
		ready := meta.FindStatusCondition(kv.Status.Conditions, conditionReady)
		if !kv.DeletionTimestamp.IsZero() || kv.Status.ObservedGeneration != kv.Generation || ready == nil || ready.Status != metav1.ConditionTrue || ready.ObservedGeneration != kv.Generation || kv.Status.Binding == nil || kv.Status.Binding.Revision == "" || kv.Status.Binding.ConfigMapName == "" || kv.Status.Binding.ConfigMapKey == "" || kv.Status.Binding.PythonHashSeed != "0" {
			return fmt.Errorf("KVService %q does not have a current Ready binding", kv.Name)
		}
		bufferBytes, err := requesterBufferBytes(kv)
		if err != nil {
			return fmt.Errorf("KVService %q requester buffer: %w", kv.Name, err)
		}
		if err := resourcevalidation.ValidateRequesterBufferBudget(pools[i].Template.Resources, bufferBytes); err != nil {
			return fmt.Errorf("modelPool %q requester buffer budget: %w", pools[i].Name, err)
		}
		pools[i].Template.KVCache.MooncakeStore = &inferencev1alpha1.NormalizedMooncakeStore{ManagedBinding: &inferencev1alpha1.ManagedMooncakeStoreBinding{Name: kv.Name, UID: string(kv.UID), BindingRevision: kv.Status.Binding.Revision, ConfigMapName: kv.Status.Binding.ConfigMapName, ConfigMapKey: kv.Status.Binding.ConfigMapKey, RequesterBufferBytes: bufferBytes}}
	}
	return nil
}

func modelServicePoolStore(service *inferencev1alpha1.ModelService, name string) *inferencev1alpha1.MooncakeStore {
	if name == "default" && len(service.Spec.ModelPools) == 0 && service.Spec.KVCache != nil {
		return service.Spec.KVCache.MooncakeStore
	}
	for i := range service.Spec.ModelPools {
		if service.Spec.ModelPools[i].Name == name && service.Spec.ModelPools[i].KVCache != nil {
			return service.Spec.ModelPools[i].KVCache.MooncakeStore
		}
	}
	return nil
}

func (reconciler *ModelServiceReconciler) modelServicesForKVService(ctx context.Context, object client.Object) []reconcile.Request {
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(object.GetNamespace())); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0)
	for i := range services.Items {
		if modelServiceReferencesKV(&services.Items[i], object.GetName()) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&services.Items[i])})
		}
	}
	return requests
}

func requesterBufferBytes(service *inferencev1alpha1.KVService) (int64, error) {
	quantity, err := resource.ParseQuantity(string(service.Spec.Requester.LocalBufferSize))
	if err != nil {
		return 0, err
	}
	bytes, exact := quantity.AsInt64()
	if !exact || bytes < 1 {
		return 0, fmt.Errorf("must be a positive exact integer byte quantity")
	}
	return bytes, nil
}
