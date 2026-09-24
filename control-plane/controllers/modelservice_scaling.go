// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves ModelService autoscaling configuration and applies capacity decisions.

package controllers

import (
	"context"
	"fmt"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type modelScalingConfig struct {
	Autoscaler      *autoscaling.Autoscaler
	Limits          core.ReplicaLimits
	PollingInterval time.Duration
	MetricsMaxAge   time.Duration
}

// scalingConfig builds the autoscaler, replica bounds, and metrics freshness for one ModelService; absent autoscaling selects fixed capacity.
func (reconciler *ModelServiceReconciler) scalingConfig(service *inferencev1alpha1.ModelService) (modelScalingConfig, error) {
	config := service.Spec.Autoscaling
	if config == nil {
		return modelScalingConfig{Autoscaler: autoscaling.Manual(), Limits: core.ReplicaLimits{MaxReplicas: maxDesiredReplicas}}, nil
	}
	if config.MinReplicas < 1 || config.MaxReplicas < config.MinReplicas {
		return modelScalingConfig{}, fmt.Errorf("autoscaling group bounds are invalid")
	}
	if config.Decision.Algorithm == "" || config.Decision.Algorithm == "manual" {
		return modelScalingConfig{}, fmt.Errorf("autoscaling decision.algorithm must select an automatic policy; omit autoscaling for fixed capacity")
	}
	selected, err := autoscaling.New(autoscaling.Configuration{
		Decision:   algorithmConfiguration(&config.Decision),
		Trigger:    algorithmConfiguration(config.Trigger),
		Adjustment: algorithmConfiguration(config.Adjustment),
		History:    reconciler.autoscalingRecommendationHistory(),
	})
	if err != nil {
		return modelScalingConfig{}, err
	}
	interval := selected.PollingInterval()
	return modelScalingConfig{
		Autoscaler:      selected,
		Limits:          core.ReplicaLimits{MinReplicas: config.MinReplicas, MaxReplicas: config.MaxReplicas},
		PollingInterval: interval,
		MetricsMaxAge:   3 * interval,
	}, nil
}

// algorithmConfiguration converts one API stage configuration to the runtime form without interpreting its parameters.
func algorithmConfiguration(config *inferencev1alpha1.ModelAutoscalingAlgorithmConfig) autoscaling.AlgorithmConfiguration {
	if config == nil {
		return autoscaling.AlgorithmConfiguration{}
	}
	result := autoscaling.AlgorithmConfiguration{Algorithm: config.Algorithm}
	if config.Parameters != nil {
		result.Parameters = config.Parameters.Raw
	}
	return result
}

// applyScaling evaluates autoscaling targets and returns compiled pools with applied capacity.
func (reconciler *ModelServiceReconciler) applyScaling(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool, scaling modelScalingConfig) ([]compiler.ModelPool, []inferencev1alpha1.AutoscalingTargetStatus, error) {
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
	// Autoscaling decisions are applied to each compiled Pool target independently.
	evaluatedAt := metav1.Now()
	snapshots := make([]core.ScalingSnapshot, 0, len(compiledPools))
	for _, compiled := range compiledPools {
		pool := byPoolName[compiled.Name]
		current := compiled.DesiredGroups
		transitioning := false
		poolUID := ""
		if pool != nil {
			current = pool.Spec.DesiredGroups
			transitioning = modelPoolTransitioning(pool)
			poolUID = string(pool.UID)
		}
		target := core.TargetID{
			ServiceNamespace: service.Namespace,
			ServiceName:      service.Name,
			ServiceUID:       string(service.UID),
			Name:             compiled.Name,
			UID:              poolUID,
			Kind:             core.TargetPool,
			Role:             autoscalingRole(compiled.Template.Role),
		}
		replicaState := modelPoolReplicaState(service, pool, groupList.Items)
		replicaState.BaselineReplicas = compiled.DesiredGroups
		replicaState.RequestedReplicas = current
		replicaState.Transitioning = replicaState.Transitioning || transitioning
		finalizeReplicaState(&replicaState)
		// A Pool not yet created has no transition; its first controller
		// write remains eligible for the selected algorithm's bootstrap core.
		if pool == nil {
			replicaState.Transitioning = false
		}
		snapshots = append(snapshots, reconciler.scalingSnapshot(ctx, service, target, evaluatedAt, replicaState, scaling))
	}
	decisions, err := scaling.Autoscaler.Plan(snapshots)
	if err != nil {
		return nil, nil, err
	}
	if len(decisions) != len(snapshots) {
		return nil, nil, fmt.Errorf("autoscaler returned %d decisions for %d scaling targets", len(decisions), len(snapshots))
	}

	bySnapshotTarget := make(map[core.TargetID]core.ScalingSnapshot, len(snapshots))
	for _, snapshot := range snapshots {
		bySnapshotTarget[snapshot.Target] = snapshot
	}
	byTarget := make(map[core.TargetID]int32, len(decisions))
	statuses := make([]inferencev1alpha1.AutoscalingTargetStatus, 0, len(decisions))
	for _, decision := range decisions {
		if _, exists := byTarget[decision.Target]; exists {
			return nil, nil, fmt.Errorf("autoscaler returned duplicate decision for target %q", decision.Target.Name)
		}
		snapshot, exists := bySnapshotTarget[decision.Target]
		if !exists {
			return nil, nil, fmt.Errorf("autoscaler returned unknown target %q", decision.Target.Name)
		}
		byTarget[decision.Target] = decision.AppliedReplicas

		var trigger *inferencev1alpha1.AutoscalingStageStatus
		if algorithm := scaling.Autoscaler.TriggerAlgorithmName(); algorithm != "" {
			trigger = &inferencev1alpha1.AutoscalingStageStatus{
				Algorithm:   algorithm,
				Disposition: string(decision.Trigger.Disposition),
				Reason:      string(decision.Trigger.Reason),
				Message:     decision.Trigger.Message,
			}
		}
		var constraint *inferencev1alpha1.AutoscalingConstraintStatus
		if decision.Constraint != "" {
			constraint = &inferencev1alpha1.AutoscalingConstraintStatus{Reason: string(decision.Constraint), Message: decision.Message}
		}
		var observationEndAt *metav1.Time
		if !snapshot.Metrics.Window.End.IsZero() {
			value := metav1.NewTime(snapshot.Metrics.Window.End)
			observationEndAt = &value
		}
		adjustmentDisposition := "Hold"
		if decision.Adjustment.Reason != core.AdjustmentReasonHold {
			adjustmentDisposition = "Apply"
		}
		statuses = append(statuses, inferencev1alpha1.AutoscalingTargetStatus{
			ID:               fmt.Sprintf("%s/%s", decision.Target.Kind, decision.Target.Name),
			Kind:             string(decision.Target.Kind),
			Role:             string(decision.Target.Role),
			EvaluatedAt:      metav1.NewTime(snapshot.EvaluatedAt),
			ObservationEndAt: observationEndAt,
			ObservationState: string(snapshot.Metrics.State),
			Trigger:          trigger,
			Decision: inferencev1alpha1.AutoscalingDecisionStatus{
				AutoscalingStageStatus: inferencev1alpha1.AutoscalingStageStatus{
					Algorithm:   decision.DecisionAlgorithm,
					Disposition: string(decision.Recommendation.State),
					Reason:      string(decision.Recommendation.Reason),
					Message:     decision.Recommendation.Message,
				},
				DesiredReplicas: decision.Recommendation.Replicas,
			},
			Adjustment: inferencev1alpha1.AutoscalingAdjustmentStatus{
				AutoscalingStageStatus: inferencev1alpha1.AutoscalingStageStatus{
					Algorithm:   decision.AdjustmentAlgorithm,
					Disposition: adjustmentDisposition,
					Reason:      string(decision.Adjustment.Reason),
					Message:     decision.Adjustment.Message,
				},
				AdjustedReplicas: decision.Adjustment.Replicas,
			},
			Constraint:       constraint,
			Direction:        string(decision.Direction),
			AppliedReplicas:  decision.AppliedReplicas,
			ReadyReplicas:    snapshot.Replicas.ReadyReplicas,
			RoutableReplicas: snapshot.Replicas.RoutableReplicas,
		})
	}
	resolved := append([]compiler.ModelPool(nil), compiledPools...)
	for index := range resolved {
		compiled := resolved[index]
		poolUID := ""
		if pool := byPoolName[compiled.Name]; pool != nil {
			poolUID = string(pool.UID)
		}
		target := core.TargetID{ServiceNamespace: service.Namespace, ServiceName: service.Name, ServiceUID: string(service.UID), Name: compiled.Name, UID: poolUID, Kind: core.TargetPool, Role: autoscalingRole(compiled.Template.Role)}
		desired, exists := byTarget[target]
		if !exists {
			return nil, nil, fmt.Errorf("autoscaler omitted target %q", target.Name)
		}
		resolved[index].DesiredGroups = desired
	}
	return resolved, statuses, nil
}

// scalingSnapshot builds one replica and metrics input for the autoscaling pipeline.
func (reconciler *ModelServiceReconciler) scalingSnapshot(ctx context.Context, service *inferencev1alpha1.ModelService, target core.TargetID, evaluatedAt metav1.Time, replicas core.ReplicaState, scaling modelScalingConfig) core.ScalingSnapshot {
	metrics := core.MetricsSnapshot{State: core.MetricsUnavailable}
	if scaling.Autoscaler.Automatic() {
		metrics = reconciler.metricsSnapshot(ctx, target, scaling.MetricsMaxAge)
	}
	return core.ScalingSnapshot{
		Target:      target,
		EvaluatedAt: evaluatedAt.Time,
		Replicas:    replicas,
		Limits:      scaling.Limits,
		Metrics:     metrics,
	}
}

// metricsSnapshot fails closed: a missing or failed provider can never be interpreted as zero demand.
func (reconciler *ModelServiceReconciler) metricsSnapshot(ctx context.Context, target core.TargetID, maxAge time.Duration) core.MetricsSnapshot {
	if reconciler.MetricsProvider == nil {
		return core.MetricsSnapshot{State: core.MetricsUnavailable}
	}
	metrics, err := reconciler.MetricsProvider.Snapshot(ctx, target)
	if err != nil || metrics.State == "" {
		return core.MetricsSnapshot{State: core.MetricsUnavailable}
	}
	if metrics.State == core.MetricsFresh {
		if metrics.Window.End.IsZero() || metrics.Window.CollectedAt.IsZero() {
			return core.MetricsSnapshot{State: core.MetricsUnavailable}
		}
		age := time.Since(metrics.Window.End)
		if age < 0 || age > maxAge {
			metrics.State = core.MetricsStale
		}
	}
	return metrics
}

func autoscalingRole(role inferencev1alpha1.ModelRole) core.TargetRole {
	switch role {
	case inferencev1alpha1.ModelRoleEncoder:
		return core.RoleEncoder
	case inferencev1alpha1.ModelRolePrefill:
		return core.RolePrefill
	case inferencev1alpha1.ModelRoleDecode:
		return core.RoleDecode
	default:
		return core.RoleAggregate
	}
}
