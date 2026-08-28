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
	Limits          core.CapacityLimits
	TriggerInterval time.Duration
	MetricsMaxAge   time.Duration
}

// scalingConfig resolves one ModelService autoscaling configuration into runtime algorithms and limits.
func (reconciler *ModelServiceReconciler) scalingConfig(service *inferencev1alpha1.ModelService) (modelScalingConfig, error) {
	config := modelScalingConfig{
		Autoscaler:      defaultAutoscaler,
		Limits:          core.CapacityLimits{MinGroups: 0, MaxGroups: maxDesiredGroups},
		TriggerInterval: defaultScalingPollInterval,
		MetricsMaxAge:   defaultMetricsMaxAge,
	}
	if reconciler.Autoscaler != nil {
		config.Autoscaler = reconciler.Autoscaler
	}
	autoscalingConfig := service.Spec.Autoscaling
	if autoscalingConfig == nil {
		return config, nil
	}
	name := autoscaling.DecisionAlgorithmName(autoscalingConfig.Algorithm)
	if autoscalingConfig.MinGroups != nil {
		config.Limits.MinGroups = *autoscalingConfig.MinGroups
	}
	if autoscalingConfig.MaxGroups != nil {
		config.Limits.MaxGroups = *autoscalingConfig.MaxGroups
	}
	config.Limits.MaxScaleUpGroups = adjustmentMaxScaleUp(autoscalingConfig.Adjustment)
	config.Limits.MaxScaleDownGroups = adjustmentMaxScaleDown(autoscalingConfig.Adjustment)
	interval, err := durationOrDefault(triggerInterval(autoscalingConfig.Trigger), defaultScalingPollInterval)
	if err != nil {
		return modelScalingConfig{}, fmt.Errorf("autoscaling trigger.interval: %w", err)
	}
	metricsMaxAge, err := durationOrDefault(autoscalingConfig.MetricsMaxAge, defaultMetricsMaxAge)
	if err != nil {
		return modelScalingConfig{}, fmt.Errorf("autoscaling metricsMaxAge: %w", err)
	}
	config.TriggerInterval = interval
	config.MetricsMaxAge = metricsMaxAge
	if reconciler.Autoscaler == nil {
		selected, err := autoscaling.New(autoscaling.Configuration{
			DecisionAlgorithm:   name,
			TriggerAlgorithm:    autoscaling.TriggerAlgorithmName(triggerAlgorithm(autoscalingConfig.Trigger)),
			AdjustmentAlgorithm: autoscaling.AdjustmentAlgorithmName(adjustmentAlgorithm(autoscalingConfig.Adjustment)),
			Decision: core.DecisionConfig{
				TargetQueuePerRoutableGroup: int64OrDefault(autoscalingConfig.TargetQueuePerRoutableGroup, 1),
				ScaleUpQueue:                int64OrDefault(autoscalingConfig.ScaleUpQueue, 1),
			},
			Trigger: core.TriggerConfig{
				LowQueuePerRoutableGroup:  triggerLowQueue(autoscalingConfig.Trigger),
				HighQueuePerRoutableGroup: triggerHighQueue(autoscalingConfig.Trigger),
			},
		})
		if err != nil {
			return modelScalingConfig{}, err
		}
		config.Autoscaler = selected
	}
	if config.Autoscaler.Automatic() && autoscalingConfig.MaxGroups == nil {
		return modelScalingConfig{}, fmt.Errorf("autoscaling maxGroups is required unless algorithm is manual")
	}
	return config, nil
}

func adjustmentAlgorithm(config *inferencev1alpha1.ModelAutoscalingAdjustmentConfig) inferencev1alpha1.AutoscalingAdjustmentAlgorithm {
	if config == nil {
		return ""
	}
	return config.Algorithm
}

func adjustmentMaxScaleUp(config *inferencev1alpha1.ModelAutoscalingAdjustmentConfig) int32 {
	if config == nil {
		return 1
	}
	return int32OrDefault(config.MaxScaleUpGroups, 1)
}

func adjustmentMaxScaleDown(config *inferencev1alpha1.ModelAutoscalingAdjustmentConfig) int32 {
	if config == nil {
		return 1
	}
	return int32OrDefault(config.MaxScaleDownGroups, 1)
}

func triggerInterval(config *inferencev1alpha1.ModelAutoscalingTriggerConfig) inferencev1alpha1.Duration {
	if config == nil {
		return ""
	}
	return config.Interval
}

func triggerAlgorithm(config *inferencev1alpha1.ModelAutoscalingTriggerConfig) inferencev1alpha1.AutoscalingTriggerAlgorithm {
	if config == nil {
		return ""
	}
	return config.Algorithm
}

func triggerLowQueue(config *inferencev1alpha1.ModelAutoscalingTriggerConfig) int64 {
	if config == nil {
		return 0
	}
	return int64OrDefault(config.LowQueuePerRoutableGroup, 0)
}

func triggerHighQueue(config *inferencev1alpha1.ModelAutoscalingTriggerConfig) int64 {
	if config == nil {
		return 1
	}
	return int64OrDefault(config.HighQueuePerRoutableGroup, 1)
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

// applyScaling evaluates autoscaling targets and returns compiled pools with applied capacity.
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

	// Ordinary Pools scale independently. Encoder, prefill, and decode instead share one
	// E/P/D pipeline-scope decision, which is applied back to all three Pool intents together.
	evaluatedAt := metav1.Now()
	snapshots := make([]core.ScalingSnapshot, 0, len(compiledPools))
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
		target := core.TargetID{
			ServiceNamespace: service.Namespace,
			ServiceName:      service.Name,
			ServiceUID:       string(service.UID),
			Name:             compiled.Name,
			UID:              poolUID,
			Kind:             core.TargetPool,
			Role:             autoscalingRole(compiled.Template.Role),
		}
		capacity := modelPoolCapacity(service, pool, groupList.Items)
		capacity.BaselineGroups = compiled.DesiredGroups
		capacity.RequestedGroups = current
		capacity.Transitioning = capacity.Transitioning || transitioning
		finalizeCapacity(&capacity)
		// A Pool not yet created has no transition; its first controller
		// write remains eligible for the selected algorithm's bootstrap core.
		if pool == nil {
			capacity.Transitioning = false
		}
		snapshots = append(snapshots, reconciler.scalingSnapshot(ctx, service, target, evaluatedAt, capacity, scaling))
	}
	if hasEPD {
		snapshot, err := reconciler.epdScalingSnapshot(ctx, service, compiledPools, epdIndexes, byPoolName, groupList.Items, evaluatedAt, scaling)
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
		byTarget[decision.Target] = decision.AppliedGroups
		reason := string(decision.DesiredCapacity.Reason)
		if decision.Constraint != "" {
			reason = string(decision.Constraint)
		}
		if decision.Trigger.Reason != "" && decision.DesiredCapacity.Reason == "" && decision.Constraint == "" {
			reason = string(decision.Trigger.Reason)
		}
		desiredGroups := decision.DesiredCapacity.Groups
		adjusted := decision.Adjustment.AdjustedGroups
		if decision.DesiredCapacity.Disposition == "" {
			desiredGroups = snapshot.Capacity.RequestedGroups
			if decision.Constraint == "" {
				adjusted = snapshot.Capacity.RequestedGroups
			}
		}
		statuses = append(statuses, inferencev1alpha1.AutoscalingTargetStatus{
			ID:                  fmt.Sprintf("%s/%s", decision.Target.Kind, decision.Target.Name),
			Kind:                string(decision.Target.Kind),
			Role:                string(decision.Target.Role),
			Algorithm:           decision.DecisionAlgorithm,
			AdjustmentAlgorithm: decision.AdjustmentAlgorithm,
			TriggerAlgorithm:    scaling.Autoscaler.TriggerAlgorithmName(),
			SnapshotID:          snapshot.ID,
			ObservedAt:          metav1.NewTime(snapshot.EvaluatedAt),
			ObservationState:    string(snapshot.Observation.State),
			Disposition:         string(decision.DesiredCapacity.Disposition),
			Reason:              reason,
			Message:             decision.Message,
			TriggerDisposition:  string(decision.Trigger.Disposition),
			TriggerReason:       string(decision.Trigger.Reason),
			TriggerMessage:      decision.Trigger.Message,
			Direction:           string(decision.Direction),
			DesiredGroups:       desiredGroups,
			AdjustmentReason:    string(decision.Adjustment.Reason),
			AdjustmentMessage:   decision.Adjustment.Message,
			AdjustedGroups:      adjusted,
			AppliedGroups:       decision.AppliedGroups,
			ReadyGroups:         snapshot.Capacity.ReadyGroups,
			RoutableGroups:      snapshot.Capacity.RoutableGroups,
		})
	}
	resolved := append([]compiler.ModelPool(nil), compiledPools...)
	for index := range resolved {
		compiled := resolved[index]
		var target core.TargetID
		if hasEPD && isEPDRole(compiled.Template.Role) {
			target = epdPipelineScopeTargetID(service)
		} else {
			poolUID := ""
			if pool := byPoolName[compiled.Name]; pool != nil {
				poolUID = string(pool.UID)
			}
			target = core.TargetID{ServiceNamespace: service.Namespace, ServiceName: service.Name, ServiceUID: string(service.UID), Name: compiled.Name, UID: poolUID, Kind: core.TargetPool, Role: autoscalingRole(compiled.Template.Role)}
		}
		desired, exists := byTarget[target]
		if !exists {
			return nil, nil, fmt.Errorf("autoscaler omitted target %q", target.Name)
		}
		resolved[index].DesiredGroups = desired
	}
	return resolved, statuses, nil
}

// scalingSnapshot builds one capacity and demand input for the autoscaling pipeline.
func (reconciler *ModelServiceReconciler) scalingSnapshot(ctx context.Context, service *inferencev1alpha1.ModelService, target core.TargetID, evaluatedAt metav1.Time, capacity core.CapacityState, scaling modelScalingConfig) core.ScalingSnapshot {
	observation := core.DemandObservation{State: core.ObservationUnavailable}
	if scaling.Autoscaler.Automatic() {
		observation = reconciler.demandObservation(ctx, target, scaling.MetricsMaxAge)
	}
	return core.ScalingSnapshot{
		ID:          fmt.Sprintf("%s/%s/%s/%s", service.UID, service.ResourceVersion, target.Kind, target.Name),
		Target:      target,
		EvaluatedAt: evaluatedAt.Time,
		Capacity:    capacity,
		Limits:      scaling.Limits,
		Observation: observation,
	}
}

// demandObservation fails closed: a missing or failed provider can never be interpreted as zero demand.
func (reconciler *ModelServiceReconciler) demandObservation(ctx context.Context, target core.TargetID, maxAge time.Duration) core.DemandObservation {
	if reconciler.PoolMetricsProvider == nil {
		return core.DemandObservation{State: core.ObservationUnavailable}
	}
	observation, err := reconciler.PoolMetricsProvider.Observation(ctx, target)
	if err != nil || observation.State == "" {
		return core.DemandObservation{State: core.ObservationUnavailable}
	}
	if observation.State == core.ObservationFresh {
		if observation.Window.End.IsZero() || observation.Window.CollectedAt.IsZero() {
			return core.DemandObservation{State: core.ObservationUnavailable}
		}
		age := time.Since(observation.Window.End)
		if age < 0 || age > maxAge {
			observation.State = core.ObservationStale
		}
	}
	return observation
}

// epdScalingSnapshot builds the shared autoscaling input for an E/P/D triplet.
func (reconciler *ModelServiceReconciler) epdScalingSnapshot(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool, indexes []int, owned map[string]*inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, evaluatedAt metav1.Time, scaling modelScalingConfig) (core.ScalingSnapshot, error) {
	if len(indexes) != 3 {
		return core.ScalingSnapshot{}, fmt.Errorf("E/P/D scaling requires exactly one encoder, prefill, and decode Pool")
	}
	seenRoles := make(map[inferencev1alpha1.ModelRole]struct{}, 3)
	baseline := pools[indexes[0]].DesiredGroups
	requested := int32(0)
	hasRequested := false
	transitioning := false
	for _, index := range indexes {
		pool := pools[index]
		if _, exists := seenRoles[pool.Template.Role]; exists {
			return core.ScalingSnapshot{}, fmt.Errorf("E/P/D scaling requires exactly one %s Pool", pool.Template.Role)
		}
		seenRoles[pool.Template.Role] = struct{}{}
		if pool.DesiredGroups != baseline {
			return core.ScalingSnapshot{}, fmt.Errorf("E/P/D scaling requires equal baseline capacity")
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
			return core.ScalingSnapshot{}, fmt.Errorf("E/P/D scaling requires a %s Pool", role)
		}
	}
	capacity := epdPipelineScopeCapacity(service, owned, groups, requested)
	capacity.BaselineGroups = baseline
	capacity.RequestedGroups = requested
	capacity.Transitioning = capacity.Transitioning || transitioning
	finalizeCapacity(&capacity)
	return reconciler.scalingSnapshot(ctx, service, epdPipelineScopeTargetID(service), evaluatedAt, capacity, scaling), nil
}

func epdPipelineScopeTargetID(service *inferencev1alpha1.ModelService) core.TargetID {
	return core.TargetID{ServiceNamespace: service.Namespace, ServiceName: service.Name, ServiceUID: string(service.UID), Name: "epd", Kind: core.TargetEPDPipelineScope, Role: core.RoleEPD}
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

func isEPDRole(role inferencev1alpha1.ModelRole) bool {
	switch role {
	case inferencev1alpha1.ModelRoleEncoder, inferencev1alpha1.ModelRolePrefill, inferencev1alpha1.ModelRoleDecode:
		return true
	default:
		return false
	}
}
