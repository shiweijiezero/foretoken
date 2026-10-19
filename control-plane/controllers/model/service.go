// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles model service intent and selects complete serving generations.

package model

import (
	"cmp"
	"context"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"sync"
	"time"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	"github.com/shiweijiezero/foretoken/control-plane/internal/observability"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
	apiequality "k8s.io/apimachinery/pkg/api/equality"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/utils/ptr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	crmetrics "sigs.k8s.io/controller-runtime/pkg/metrics"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	modelServiceFinalizer      = "inference.foretoken.io/modelservice-protection"
	conditionIntentCompiled    = "IntentCompiled"
	conditionPoolsMaterialized = "PoolsMaterialized"
	maxDesiredReplicas         = int32(1<<31 - 1)
)

// ScalingMetricsProvider supplies one read-only, target-attributed metrics snapshot.
// Implementations collect metrics without mutating Kubernetes resources or autoscaling state.
type ScalingMetricsProvider interface {
	Snapshot(context.Context, core.TargetID, bool) (core.MetricsSnapshot, error)
}

// ServiceReconciler compiles ModelService intent and owns ModelPool specs.
type ServiceReconciler struct {
	client.Client
	APIReader                client.Reader
	MetricsProvider          ScalingMetricsProvider
	CacheProfile             runtimeconfig.RuntimeCacheProfile
	SourceMode               bool
	RuntimeProfile           resolver.RuntimeProfile
	ApplicationFiles         runtimeconfig.ApplicationFiles
	ApplicationURL           string
	HuggingFaceAccessProfile runtimeconfig.HuggingFaceAccessProfile
	Alerts                   *observability.ServiceAlerts

	recommendationHistoryOnce sync.Once
	recommendationHistory     *core.RecommendationHistory
}

func (reconciler *ServiceReconciler) autoscalingRecommendationHistory() *core.RecommendationHistory {
	reconciler.recommendationHistoryOnce.Do(func() {
		reconciler.recommendationHistory = core.NewRecommendationHistory()
	})
	return reconciler.recommendationHistory
}

// SetupWithManager registers the ModelService controller and its owned resources.
func (reconciler *ServiceReconciler) SetupWithManager(manager ctrl.Manager) error {
	reconciler.APIReader = manager.GetAPIReader()
	builder := ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.ModelService{}).
		Owns(&inferencev1alpha1.ModelPool{}).
		Watches(&corev1.ConfigMap{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesForInstanceConfiguration)).
		Watches(&inferencev1alpha1.ModelGroup{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesForGroup)).
		Watches(&inferencev1alpha1.KVService{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesForKVService)).
		Watches(&inferencev1alpha1.RuntimeCache{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesInNamespace)).
		Watches(&inferencev1alpha1.FrontendService{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesInNamespace))
	if reconciler.CacheProfile.ClaimName != "" {
		builder = builder.Watches(&corev1.PersistentVolumeClaim{}, handler.EnqueueRequestsFromMapFunc(func(ctx context.Context, object client.Object) []reconcile.Request {
			if object.GetName() != reconciler.CacheProfile.ClaimName {
				return nil
			}
			return reconciler.modelServicesInNamespace(ctx, object)
		}))
	}
	if reconciler.Alerts != nil && reconciler.Alerts.WatchRules {
		builder = builder.Owns(&monitoringv1.PrometheusRule{})
	}
	if err := builder.Complete(reconciler); err != nil {
		return err
	}
	return crmetrics.Registry.Register(newAutoscalingCollector(manager.GetCache()))
}

// Reconcile materializes stable ModelPools and aggregates their serving readiness.
func (reconciler *ServiceReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	service := new(inferencev1alpha1.ModelService)
	if err := reconciler.Get(ctx, request.NamespacedName, service); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	result, err := reconciler.reconcileService(ctx, service)
	return result, errors.Join(err, reconciler.reconcileAlerts(ctx, service))
}

// reconcileService keeps model lifecycle and serving readiness independent of optional alert delivery.
func (reconciler *ServiceReconciler) reconcileService(ctx context.Context, service *inferencev1alpha1.ModelService) (ctrl.Result, error) {
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

	// Explicit suspension overrides capacity policies and startup configuration.
	// Retire the existing execution contracts without rewriting the user's intent.
	if service.Spec.Suspend {
		if err := reconciler.retirePoolCapacity(ctx, service, nil, true); err != nil {
			return ctrl.Result{}, err
		}
		return reconciler.reconcileStoppedService(ctx, service, nil, nil)
	}

	compiledPools, err := compiler.CompileModelService(service.Spec)
	if err != nil {
		return ctrl.Result{}, reconciler.rejectTarget(ctx, service, modelServiceState{
			compiled: metav1.Condition{Status: metav1.ConditionFalse, Reason: "InvalidIntent", Message: err.Error()},
			pools:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "CompilationFailed", Message: "ModelService intent was not compiled"},
			target:   metav1.Condition{Status: metav1.ConditionFalse, Reason: "InvalidIntent", Message: err.Error()},
			ready:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "InvalidIntent", Message: "ModelService intent is invalid"},
		})
	}
	scaling, err := reconciler.scalingConfig(service)
	var autoscalingStatus []inferencev1alpha1.AutoscalingTargetStatus
	if err == nil {
		compiledPools, autoscalingStatus, err = reconciler.applyScaling(ctx, service, compiledPools, scaling)
	}
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: metav1.Condition{Status: metav1.ConditionFalse, Reason: "ScalingFailed", Message: err.Error()},
			pools:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "ScalingFailed", Message: "ModelPool capacity was not resolved"},
			ready:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "ScalingFailed", Message: "ModelService capacity is invalid"},
		})
	}
	// Retire existing capacity using its selected execution contract before resolving
	// dependencies needed to start or replace runtimes.
	stopping := !slices.ContainsFunc(compiledPools, func(pool compiler.ModelPool) bool { return pool.DesiredGroups > 0 })
	if err := reconciler.retirePoolCapacity(ctx, service, compiledPools, stopping); err != nil {
		return ctrl.Result{}, err
	}
	if stopping {
		return reconciler.reconcileStoppedService(ctx, service, compiledPools, autoscalingStatus)
	}
	sourceAllowed, err := reconciler.sourceSelectionAllowed(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	sourceRevision, err := runtimeconfig.SourceRevision(service.Annotations, sourceAllowed)
	if err != nil {
		return ctrl.Result{}, reconciler.rejectTarget(ctx, service, modelServiceState{
			compiled: metav1.Condition{Status: metav1.ConditionFalse, Reason: "InvalidIntent", Message: err.Error()},
			pools:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "ResolutionFailed", Message: "Source application selection is invalid"},
			target:   metav1.Condition{Status: metav1.ConditionFalse, Reason: "InvalidIntent", Message: err.Error()},
			ready:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "InvalidIntent", Message: err.Error()},
		})
	}
	if err := reconciler.resolveManagedKVBindings(ctx, service, compiledPools); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: metav1.Condition{Status: metav1.ConditionFalse, Reason: "KVServiceNotReady", Message: err.Error()},
			pools:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "ResolutionFailed", Message: "No new ModelPools were materialized"},
			ready:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "KVServiceNotReady", Message: "Referenced KVService is not ready"},
		})
	}
	runtimeCache, cacheReady, err := reconciler.CacheProfile.Resolve(ctx, reconciler.Client, service.Namespace)
	if err != nil {
		statusErr := reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: metav1.Condition{Status: metav1.ConditionTrue, Reason: "Compiled", Message: "ModelService intent was compiled"},
			pools:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "CacheResolutionFailed", Message: "Runtime cache could not be resolved"},
			ready:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "CacheResolutionFailed", Message: err.Error()},
		})
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	if !cacheReady {
		ready, reason, message, readinessErr := reconciler.serviceReadiness(ctx, service, compiledPools)
		if ready && service.Status.ServingGeneration == service.Generation {
			ready, reason, message, readinessErr = reconciler.instanceAdmissionApplied(ctx, service, compiledPools)
		} else if ready {
			reason, message = "ServingPreviousGeneration", "The previous complete ModelService generation remains ready while runtime cache storage is preparing"
		}
		statusErr := reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: metav1.Condition{Status: metav1.ConditionTrue, Reason: "Compiled", Message: "ModelService intent was compiled"},
			pools:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "CacheNotReady", Message: "No new ModelPools were materialized"},
			ready:    metav1.Condition{Status: workload.ConditionStatus(ready), Reason: reason, Message: message},
		})
		result := ctrl.Result{}
		if reason == "ConfigurationPending" || reason == "ConfigurationRejected" {
			result.RequeueAfter = time.Second
		}
		return result, errors.Join(readinessErr, statusErr)
	}
	huggingFaceAccess := reconciler.HuggingFaceAccessProfile.Access()
	for index := range compiledPools {
		compiledPools[index].Template.RuntimeCache = runtimeCache.DeepCopy()
		compiledPools[index].Template.SourceRevision = sourceRevision
		if compiledPools[index].Template.Source == inferencev1alpha1.ModelSourceHF {
			compiledPools[index].Template.HuggingFaceAccess = huggingFaceAccess.DeepCopy()
		}
	}

	if err := reconciler.reconcilePools(ctx, service, compiledPools); err != nil {
		var deploymentRequired *instanceAdmissionDeploymentRequiredError
		if errors.As(err, &deploymentRequired) {
			return ctrl.Result{}, reconciler.rejectTarget(ctx, service, modelServiceState{
				compiled: metav1.Condition{Status: metav1.ConditionTrue, Reason: "Compiled", Message: "ModelService intent was compiled"},
				pools:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "DeploymentRequired", Message: err.Error()},
				target:   metav1.Condition{Status: metav1.ConditionFalse, Reason: "DeploymentRequired", Message: err.Error()},
				ready:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "DeploymentRequired", Message: err.Error()},
			})
		}
		statusErr := reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: metav1.Condition{Status: metav1.ConditionTrue, Reason: "Compiled", Message: "ModelService intent was compiled"},
			pools:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "ApplyFailed", Message: "ModelPools were not fully materialized"},
			ready:    metav1.Condition{Status: metav1.ConditionFalse, Reason: "PoolsNotReady", Message: "ModelPools are not ready"},
		})
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	if _, err := reconciler.commitServingGeneration(ctx, service, compiledPools); err != nil {
		return ctrl.Result{}, err
	}
	ready, readyReason, readyMessage, err := reconciler.serviceReadiness(ctx, service, compiledPools)
	if err != nil {
		return ctrl.Result{}, err
	}
	if ready && service.Status.ServingGeneration == service.Generation {
		ready, readyReason, readyMessage, err = reconciler.instanceAdmissionApplied(ctx, service, compiledPools)
		if err != nil {
			return ctrl.Result{}, err
		}
	}
	if ready && service.Status.ServingGeneration == service.Generation {
		ready, readyReason, readyMessage, err = servingconfig.ModelConfigurationApplied(ctx, reconciler.Client, reconciler.APIReader, service)
		if err != nil {
			return ctrl.Result{}, err
		}
	}
	target, err := reconciler.targetReadiness(ctx, service, compiledPools)
	if err != nil {
		return ctrl.Result{}, err
	}
	pools := metav1.Condition{Status: metav1.ConditionTrue, Reason: "Applied", Message: "All ModelPools were materialized"}
	if err := reconciler.updateStatus(ctx, service, modelServiceState{
		compiled:    metav1.Condition{Status: metav1.ConditionTrue, Reason: "Compiled", Message: "ModelService intent was compiled"},
		pools:       pools,
		target:      target,
		ready:       metav1.Condition{Status: workload.ConditionStatus(ready), Reason: readyReason, Message: readyMessage},
		autoscaling: &autoscalingStatus,
	}); err != nil {
		return ctrl.Result{}, err
	}
	if readyReason == "ConfigurationPending" || readyReason == "ConfigurationRejected" {
		return ctrl.Result{RequeueAfter: time.Second}, nil
	}
	if scaling.Autoscaler.Automatic() {
		return ctrl.Result{RequeueAfter: scaling.PollingInterval}, nil
	}
	return ctrl.Result{}, nil
}

// retirePoolCapacity applies decreases without replacing the Pools' execution selections.
// A full stop also retires obsolete Pools retained by an unfinished rollout.
func (reconciler *ServiceReconciler) retirePoolCapacity(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool, stopping bool) error {
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return err
	}
	desired := make(map[string]int32, len(compiledPools))
	for _, compiled := range compiledPools {
		desired[compiled.Name] = compiled.DesiredGroups
	}
	for index := range pools {
		pool := &pools[index]
		capacity, selected := desired[pool.Spec.PoolName]
		if (!selected && !stopping) || !pool.DeletionTimestamp.IsZero() || capacity >= pool.Spec.DesiredGroups {
			continue
		}
		base := pool.DeepCopy()
		pool.Spec.DesiredGroups = capacity
		if err := reconciler.Patch(ctx, pool, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return fmt.Errorf("retire ModelPool %q capacity: %w", pool.Name, err)
		}
	}
	return nil
}

// reconcileStoppedService withdraws the serving cohort and waits for Group-owned execution to end.
// Application selections remain available for a later resume without preparing new runtimes.
func (reconciler *ServiceReconciler) reconcileStoppedService(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool, autoscaling []inferencev1alpha1.AutoscalingTargetStatus) (ctrl.Result, error) {
	if _, err := reconciler.commitServingGeneration(ctx, service, compiledPools); err != nil {
		return ctrl.Result{}, err
	}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	stopped := true
	for index := range pools {
		groups, err := ownedModelGroups(ctx, reader, &pools[index])
		if err != nil {
			return ctrl.Result{}, err
		}
		stopped = stopped && modelPoolStopped(&pools[index]) && len(groups) == 0
	}
	target := metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Stopping", Message: "Waiting for model execution to stop"}
	if stopped {
		target = metav1.Condition{Status: metav1.ConditionTrue, Reason: "ScaledToZero", Message: "All model execution has stopped"}
	}
	compiled := metav1.Condition{Status: metav1.ConditionTrue, Reason: "Compiled", Message: "ModelService capacity was compiled"}
	if service.Spec.Suspend {
		compiled = metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Suspended", Message: "Startup configuration is retained while model execution is suspended"}
	}
	err = reconciler.updateStatus(ctx, service, modelServiceState{
		compiled:    compiled,
		pools:       metav1.Condition{Status: metav1.ConditionTrue, Reason: "Applied", Message: "Existing ModelPools have no requested capacity"},
		target:      target,
		ready:       metav1.Condition{Status: metav1.ConditionFalse, Reason: target.Reason, Message: target.Message},
		autoscaling: &autoscaling,
	})
	if !stopped {
		return ctrl.Result{RequeueAfter: time.Second}, err
	}
	return ctrl.Result{}, err
}

// rejectTarget publishes a configuration rejection without withdrawing a still-serving cohort.
func (reconciler *ServiceReconciler) rejectTarget(ctx context.Context, service *inferencev1alpha1.ModelService, state modelServiceState) error {
	if len(service.Status.ServingPoolRevisions) > 0 {
		ready, reason, message, err := reconciler.serviceReadiness(ctx, service, nil)
		if err != nil {
			return err
		}
		state.ready = metav1.Condition{Status: workload.ConditionStatus(ready), Reason: reason, Message: message}
	}
	return reconciler.updateStatus(ctx, service, state)
}

// reconcilePools converges compiled ModelPool contracts and retains service-selected serving pools.
func (reconciler *ServiceReconciler) reconcilePools(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) error {
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

	if err := reconciler.selectPoolApplications(ctx, service, compiledPools, byPoolName); err != nil {
		return err
	}

	capacityBlocked := replacementPoolsCapacityBlocked(compiledPools, byPoolName)
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
			ModelServiceRef:   inferencev1alpha1.LocalObjectReference{Name: service.Name, UID: string(service.UID)},
			PoolName:          compiled.Name,
			DesiredGroups:     compiled.DesiredGroups,
			MaxInputTokens:    compiled.MaxInputTokens,
			InstanceAdmission: compiled.InstanceAdmission.DeepCopy(),
			Template:          compiled.Template,
		}
		if compiled.MaxInputTokens != nil {
			pool.Spec.MaxInputTokens = ptr.To(*compiled.MaxInputTokens)
		}
		if err := controllerutil.SetControllerReference(service, pool, reconciler.Scheme()); err != nil {
			return fmt.Errorf("set ModelPool %q owner: %w", compiled.Name, err)
		}

		if created {
			if err := reconciler.Create(ctx, pool); err != nil {
				return fmt.Errorf("create ModelPool %q: %w", compiled.Name, err)
			}
		} else if !apiequality.Semantic.DeepEqual(before.Spec, pool.Spec) || !reflect.DeepEqual(before.OwnerReferences, pool.OwnerReferences) {
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
		// Retain obsolete serving Pools while replacements can schedule. Under capacity
		// pressure, their normal deletion drain releases resources for the new topology.
		if servingconfig.ServingRevision(service, pool) != "" && !capacityBlocked {
			continue
		}
		if err := reconciler.retirePool(ctx, pool); err != nil {
			return fmt.Errorf("delete stale ModelPool %q: %w", pool.Name, err)
		}
	}
	return nil
}

// replacementPoolsCapacityBlocked permits obsolete-Pool retirement only after every active
// replacement has resolved current intent and at least one reports scheduler capacity pressure.
// Failed or unobserved targets retain the serving topology rather than spending its capacity.
func replacementPoolsCapacityBlocked(compiledPools []compiler.ModelPool, pools map[string]*inferencev1alpha1.ModelPool) bool {
	blocked := false
	for _, compiled := range compiledPools {
		if compiled.DesiredGroups == 0 {
			continue
		}
		pool := pools[compiled.Name]
		if pool == nil || !pool.DeletionTimestamp.IsZero() || pool.Status.ObservedGeneration != pool.Generation ||
			pool.Spec.DesiredGroups != compiled.DesiredGroups || !ptr.Equal(pool.Spec.MaxInputTokens, compiled.MaxInputTokens) ||
			!reflect.DeepEqual(pool.Spec.InstanceAdmission, compiled.InstanceAdmission) || !apiequality.Semantic.DeepEqual(pool.Spec.Template, compiled.Template) {
			return false
		}
		for _, name := range []string{conditionResolved, workload.ConditionGroupsMaterialized} {
			condition := meta.FindStatusCondition(pool.Status.Conditions, name)
			if condition == nil || condition.ObservedGeneration != pool.Generation || condition.Status != metav1.ConditionTrue {
				return false
			}
		}
		target := meta.FindStatusCondition(pool.Status.Conditions, workload.ConditionTargetReady)
		if target == nil || target.ObservedGeneration != pool.Generation || target.Status == metav1.ConditionFalse {
			return false
		}
		rollout := meta.FindStatusCondition(pool.Status.Conditions, conditionRolloutPending)
		if rollout != nil && rollout.ObservedGeneration == pool.Generation && rollout.Status == metav1.ConditionTrue && rollout.Reason == "InsufficientCapacity" {
			blocked = true
		}
	}
	return blocked
}

// commitServingGeneration atomically selects only fully prepared ModelPool revisions for frontend routing.
func (reconciler *ServiceReconciler) commitServingGeneration(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) (bool, error) {
	// Keep routing on the last complete cohort until every nonzero Pool has prepared a
	// compatible revision. The final status patch is the service-level atomic commit point.
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return false, err
	}
	byName := make(map[string]*inferencev1alpha1.ModelPool, len(pools))
	for index := range pools {
		pool := &pools[index]
		byName[pool.Spec.PoolName] = pool
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		return false, fmt.Errorf("list ModelGroups for serving generation: %w", err)
	}
	selected := make([]inferencev1alpha1.ServingPoolRevision, 0, len(compiledPools))
	for _, compiled := range compiledPools {
		pool := byName[compiled.Name]
		if compiled.DesiredGroups == 0 {
			if pool != nil && !modelPoolStopped(pool) {
				return false, nil
			}
			continue
		}
		if pool == nil || pool.Spec.DesiredGroups != compiled.DesiredGroups || !ptr.Equal(pool.Spec.MaxInputTokens, compiled.MaxInputTokens) || !reflect.DeepEqual(pool.Spec.InstanceAdmission, compiled.InstanceAdmission) || !apiequality.Semantic.DeepEqual(pool.Spec.Template, compiled.Template) || pool.Status.ObservedGeneration != pool.Generation || pool.Status.PreparedRevision == "" || !poolRevisionReady(groups.Items, pool, pool.Status.PreparedRevision, pool.Spec.DesiredGroups) {
			return false, nil
		}
		selected = append(selected, inferencev1alpha1.ServingPoolRevision{PoolName: pool.Spec.PoolName, PoolUID: string(pool.UID), Revision: pool.Status.PreparedRevision})
	}
	slices.SortFunc(selected, func(left, right inferencev1alpha1.ServingPoolRevision) int {
		return cmp.Compare(left.PoolName, right.PoolName)
	})
	candidate := service.DeepCopy()
	candidate.Status.ServingPoolRevisions = selected
	servicePools := servingconfig.OwnedRoutingPools(candidate, pools)
	servicePools = slices.DeleteFunc(servicePools, func(pool *inferencev1alpha1.ModelPool) bool {
		return servingconfig.ServingRevision(candidate, pool) == ""
	})
	if len(selected) > 0 {
		switch {
		case servingconfig.PoolsHaveEPD(servicePools) || servingconfig.ServiceDeclaresEPD(candidate):
			if !servingconfig.PoolsContainCompleteEPD(servicePools) {
				return false, nil
			}
			if _, _, err := servingconfig.ProjectServiceEPDComponents(candidate, servicePools, groups.Items); err != nil {
				return false, err
			}
		case servingconfig.PoolsHavePD(servicePools):
			if _, _, err := servingconfig.ProjectServicePDComponents(candidate, servicePools, groups.Items); err != nil {
				return false, err
			}
		default:
			routes := make([]servingconfig.Group, 0)
			for _, pool := range servicePools {
				revision := servingconfig.ServingRevision(candidate, pool)
				for index := range groups.Items {
					group := &groups.Items[index]
					if servingconfig.RoutingGroupOwnedBy(group, pool) && group.Spec.Revision == revision && servingconfig.RoutingGroupReady(group) && group.Spec.Role == inferencev1alpha1.ModelRoleAggregate {
						routes = append(routes, servingconfig.RoutingGroupForService(candidate, pool, group))
					}
				}
			}
			if err := servingconfig.ValidateRoutingIdentities(nil, routes, nil, nil); err != nil {
				return false, err
			}
		}
	}
	if service.Status.ServingGeneration == service.Generation && slices.Equal(service.Status.ServingPoolRevisions, selected) {
		return true, nil
	}
	base := service.DeepCopy()
	service.Status.ServingGeneration = service.Generation
	service.Status.ServingPoolRevisions = selected
	if err := reconciler.Status().Patch(ctx, service, client.MergeFrom(base)); err != nil {
		return false, fmt.Errorf("commit ModelService serving generation: %w", err)
	}
	return true, nil
}

// targetReadiness observes current requested execution independently of the serving cohort.
// Matching the complete intent prevents an older Pool failure from rejecting a new deployment.
func (reconciler *ServiceReconciler) targetReadiness(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) (metav1.Condition, error) {
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return metav1.Condition{}, err
	}
	state := metav1.Condition{Status: metav1.ConditionTrue, Reason: "Ready", Message: "All requested ModelPools are ready"}
	for _, compiled := range compiledPools {
		var target *metav1.Condition
		for index := range pools {
			pool := &pools[index]
			if pool.Spec.PoolName != compiled.Name || !pool.DeletionTimestamp.IsZero() ||
				pool.Spec.DesiredGroups != compiled.DesiredGroups || !ptr.Equal(pool.Spec.MaxInputTokens, compiled.MaxInputTokens) ||
				!reflect.DeepEqual(pool.Spec.InstanceAdmission, compiled.InstanceAdmission) || !apiequality.Semantic.DeepEqual(pool.Spec.Template, compiled.Template) ||
				pool.Status.ObservedGeneration != pool.Generation {
				continue
			}
			condition := meta.FindStatusCondition(pool.Status.Conditions, workload.ConditionTargetReady)
			if condition != nil && condition.ObservedGeneration == pool.Generation {
				target = condition
			}
		}
		if target != nil && target.Status == metav1.ConditionFalse {
			return metav1.Condition{Status: metav1.ConditionFalse, Reason: target.Reason, Message: fmt.Sprintf("ModelPool %s: %s", compiled.Name, target.Message)}, nil
		}
		if target == nil || target.Status != metav1.ConditionTrue {
			state = metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Reconciling", Message: "Waiting for requested ModelPools to become ready"}
		}
	}
	return state, nil
}

// serviceReadiness verifies that the selected serving generation remains routable.
func (reconciler *ServiceReconciler) serviceReadiness(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) (bool, string, string, error) {
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return false, "PoolsNotReady", "ModelPools are not ready", err
	}
	hasRequestedCapacity := slices.ContainsFunc(compiledPools, func(pool compiler.ModelPool) bool { return pool.DesiredGroups > 0 })
	if len(service.Status.ServingPoolRevisions) == 0 {
		if !hasRequestedCapacity {
			return false, "ScaledToZero", "ModelService has no requested serving capacity", nil
		}
		return false, "PoolsNotReady", "No complete ModelPool generation is ready", nil
	}
	byPoolName := make(map[string]*inferencev1alpha1.ModelPool, len(pools))
	for index := range pools {
		pool := &pools[index]
		byPoolName[pool.Spec.PoolName] = pool
	}
	selectedPools := make([]*inferencev1alpha1.ModelPool, 0, len(service.Status.ServingPoolRevisions))
	for _, selected := range service.Status.ServingPoolRevisions {
		pool := byPoolName[selected.PoolName]
		if pool == nil || string(pool.UID) != selected.PoolUID || servingconfig.ServingRevision(service, pool) != selected.Revision {
			return false, "PoolsNotReady", "One or more serving ModelPool revisions are unavailable", nil
		}
		selectedPools = append(selectedPools, pool)
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		return false, "PoolsNotReady", "ModelGroups are not ready", err
	}
	switch {
	case servingconfig.PoolsHaveEPD(selectedPools):
		if _, _, err := servingconfig.ProjectServiceEPDComponents(service, selectedPools, groups.Items); err != nil {
			return false, "PoolsNotReady", "The serving E/P/D generation is incomplete", nil
		}
	case servingconfig.PoolsHavePD(selectedPools):
		if _, _, err := servingconfig.ProjectServicePDComponents(service, selectedPools, groups.Items); err != nil {
			return false, "PoolsNotReady", "The serving P/D generation is incomplete", nil
		}
	default:
		for _, pool := range selectedPools {
			if !poolRevisionServingReady(groups.Items, pool, servingconfig.ServingRevision(service, pool)) {
				return false, "PoolsNotReady", "One or more serving ModelPool revisions are not ready", nil
			}
		}
	}
	if service.Status.ServingGeneration != service.Generation {
		return true, "ServingPreviousGeneration", "The previous complete ModelService generation remains ready while the new generation is preparing", nil
	}
	return true, "Ready", "All serving ModelPools are ready", nil
}

func poolRevisionReady(groups []inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool, revision string, desired int32) bool {
	if revision == "" || desired == 0 {
		return false
	}
	ready := make(map[int32]struct{}, desired)
	for index := range groups {
		group := &groups[index]
		if !servingconfig.RoutingGroupOwnedBy(group, pool) || group.Spec.Revision != revision || group.Spec.Ordinal >= desired || !servingconfig.RoutingGroupReady(group) {
			continue
		}
		if _, duplicate := ready[group.Spec.Ordinal]; duplicate {
			return false
		}
		ready[group.Spec.Ordinal] = struct{}{}
	}
	return int32(len(ready)) == desired
}

func poolRevisionServingReady(groups []inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool, revision string) bool {
	return revision != "" && slices.ContainsFunc(groups, func(group inferencev1alpha1.ModelGroup) bool {
		return servingconfig.RoutingGroupOwnedBy(&group, pool) && group.Spec.Revision == revision && servingconfig.RoutingGroupReady(&group)
	})
}

func modelPoolReady(pool *inferencev1alpha1.ModelPool) bool {
	if pool == nil || !pool.DeletionTimestamp.IsZero() || pool.Status.ObservedGeneration != pool.Generation {
		return false
	}
	condition := meta.FindStatusCondition(pool.Status.Conditions, workload.ConditionReady)
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

// retirePool retains the Pool until its Groups have completed execution retirement.
func (reconciler *ServiceReconciler) retirePool(ctx context.Context, pool *inferencev1alpha1.ModelPool) error {
	if !pool.DeletionTimestamp.IsZero() {
		return nil
	}
	if !controllerutil.ContainsFinalizer(pool, modelPoolFinalizer) {
		base := pool.DeepCopy()
		controllerutil.AddFinalizer(pool, modelPoolFinalizer)
		if err := reconciler.Patch(ctx, pool, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return fmt.Errorf("protect ModelPool retirement: %w", err)
		}
	}
	return client.IgnoreNotFound(reconciler.Delete(ctx, pool, client.Preconditions{UID: &pool.UID}))
}

// reconcileDelete removes owned ModelPools before releasing the ModelService finalizer.
func (reconciler *ServiceReconciler) reconcileDelete(ctx context.Context, service *inferencev1alpha1.ModelService) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(service, modelServiceFinalizer) {
		return ctrl.Result{}, nil
	}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	if len(pools) > 0 {
		for index := range pools {
			if err := reconciler.retirePool(ctx, &pools[index]); err != nil {
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

// ownedPools returns ModelPools whose reference and controller owner both identify the ModelService.
func (reconciler *ServiceReconciler) ownedPools(ctx context.Context, service *inferencev1alpha1.ModelService) ([]inferencev1alpha1.ModelPool, error) {
	var list inferencev1alpha1.ModelPoolList
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	if err := reader.List(ctx, &list, client.InNamespace(service.Namespace)); err != nil {
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

type modelServiceState struct {
	compiled    metav1.Condition
	pools       metav1.Condition
	target      metav1.Condition
	ready       metav1.Condition
	autoscaling *[]inferencev1alpha1.AutoscalingTargetStatus
}

// updateStatus publishes compilation, pool, readiness, and autoscaling results for the ModelService.
func (reconciler *ServiceReconciler) updateStatus(ctx context.Context, service *inferencev1alpha1.ModelService, state modelServiceState) error {
	base := service.DeepCopy()
	service.Status.ObservedGeneration = service.Generation
	if state.target.Status == "" {
		state.target = metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Reconciling", Message: "Waiting for requested ModelPools to be materialized"}
	}
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: workload.ConditionTargetReady, Status: state.target.Status, Reason: state.target.Reason, Message: state.target.Message, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionIntentCompiled, Status: state.compiled.Status, Reason: state.compiled.Reason, Message: state.compiled.Message, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: conditionPoolsMaterialized, Status: state.pools.Status, Reason: state.pools.Reason, Message: state.pools.Message, ObservedGeneration: service.Generation})
	meta.SetStatusCondition(&service.Status.Conditions, metav1.Condition{Type: workload.ConditionReady, Status: state.ready.Status, Reason: state.ready.Reason, Message: state.ready.Message, ObservedGeneration: service.Generation})
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
// current binding configuration before any ModelPool is created or changed.
func (reconciler *ServiceReconciler) resolveManagedKVBindings(ctx context.Context, service *inferencev1alpha1.ModelService, pools []compiler.ModelPool) error {
	for i := range pools {
		store := modelServicePoolStore(service, pools[i].Name)
		if store == nil || store.KVServiceRef == nil {
			continue
		}
		kv := new(inferencev1alpha1.KVService)
		if err := reconciler.Get(ctx, client.ObjectKey{Namespace: service.Namespace, Name: store.KVServiceRef.Name}, kv); err != nil {
			return fmt.Errorf("get KVService %q: %w", store.KVServiceRef.Name, err)
		}
		ready := meta.FindStatusCondition(kv.Status.Conditions, workload.ConditionReady)
		if !kv.DeletionTimestamp.IsZero() || kv.Status.ObservedGeneration != kv.Generation || ready == nil || ready.Status != metav1.ConditionTrue || ready.ObservedGeneration != kv.Generation || kv.Status.Binding == nil || kv.Status.Binding.Revision == "" || kv.Status.Binding.ConfigMapName == "" || kv.Status.Binding.ConfigMapKey == "" || kv.Status.Binding.PythonHashSeed != "0" {
			return fmt.Errorf("KVService %q does not have a current Ready binding", kv.Name)
		}
		bufferBytes, err := resourcevalidation.ParsePositiveBytes("requester.localBufferSize", string(kv.Spec.Requester.LocalBufferSize))
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

// modelServicesForGroup refreshes alert scope when an execution group is created or removed.
func (reconciler *ServiceReconciler) modelServicesForGroup(ctx context.Context, object client.Object) []reconcile.Request {
	group := object.(*inferencev1alpha1.ModelGroup)
	pool := new(inferencev1alpha1.ModelPool)
	if err := reconciler.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: group.Spec.ModelPoolRef.Name}, pool); err != nil {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: pool.Namespace, Name: pool.Spec.ModelServiceRef.Name}}}
}

// modelServicesInNamespace maps shared platform resource changes to every ModelService in the namespace.
func (reconciler *ServiceReconciler) modelServicesInNamespace(ctx context.Context, object client.Object) []reconcile.Request {
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(object.GetNamespace())); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0, len(services.Items))
	for index := range services.Items {
		requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&services.Items[index])})
	}
	return requests
}

// modelServicesForKVService maps a KVService update to ModelServices that reference it.
func (reconciler *ServiceReconciler) modelServicesForKVService(ctx context.Context, object client.Object) []reconcile.Request {
	var services inferencev1alpha1.ModelServiceList
	if err := reconciler.List(ctx, &services, client.InNamespace(object.GetNamespace())); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0)
	for i := range services.Items {
		if runtimeconfig.ModelServiceReferencesKV(&services.Items[i], object.GetName()) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(&services.Items[i])})
		}
	}
	return requests
}
