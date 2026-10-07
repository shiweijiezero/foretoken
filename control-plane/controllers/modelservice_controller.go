// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles ModelService intent into controller-owned ModelPool resources.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"slices"
	"sync"

	monitoringv1 "github.com/prometheus-operator/prometheus-operator/pkg/apis/monitoring/v1"
	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/autoscaling/core"
	"github.com/shiweijiezero/foretoken/control-plane/internal/compiler"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	resourcevalidation "github.com/shiweijiezero/foretoken/control-plane/internal/resources"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
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
	conditionReady             = "Ready"
	maxDesiredReplicas         = int32(1<<31 - 1)
)

// ScalingMetricsProvider supplies one read-only, target-attributed metrics snapshot.
// Implementations collect metrics without mutating Kubernetes resources or autoscaling state.
type ScalingMetricsProvider interface {
	Snapshot(context.Context, core.TargetID) (core.MetricsSnapshot, error)
}

// ModelServiceReconciler compiles ModelService intent and owns ModelPool specs.
type ModelServiceReconciler struct {
	client.Client
	MetricsProvider          ScalingMetricsProvider
	CacheProfile             RuntimeCacheProfile
	SourceMode               bool
	RuntimeProfile           resolver.RuntimeProfile
	ApplicationFiles         runtimeconfig.ApplicationFiles
	ApplicationURL           string
	HuggingFaceAccessProfile HuggingFaceAccessProfile
	Alerts                   *ServiceAlerts

	recommendationHistoryOnce sync.Once
	recommendationHistory     *core.RecommendationHistory
}

func (reconciler *ModelServiceReconciler) autoscalingRecommendationHistory() *core.RecommendationHistory {
	reconciler.recommendationHistoryOnce.Do(func() {
		reconciler.recommendationHistory = core.NewRecommendationHistory()
	})
	return reconciler.recommendationHistory
}

// SetupWithManager registers the ModelService controller and its owned resources.
func (reconciler *ModelServiceReconciler) SetupWithManager(manager ctrl.Manager) error {
	builder := ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.ModelService{}).
		Owns(&inferencev1alpha1.ModelPool{}).
		Watches(&inferencev1alpha1.ModelGroup{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesForGroup)).
		Watches(&inferencev1alpha1.KVService{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesForKVService)).
		Watches(&inferencev1alpha1.RuntimeCache{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelServicesInNamespace))
	if reconciler.Alerts != nil && reconciler.Alerts.watchRules {
		builder = builder.Owns(&monitoringv1.PrometheusRule{})
	}
	if err := builder.Complete(reconciler); err != nil {
		return err
	}
	return crmetrics.Registry.Register(newAutoscalingCollector(manager.GetCache()))
}

// Reconcile materializes stable ModelPools and aggregates their serving readiness.
func (reconciler *ModelServiceReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	service := new(inferencev1alpha1.ModelService)
	if err := reconciler.Get(ctx, request.NamespacedName, service); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	result, err := reconciler.reconcileService(ctx, service)
	return result, errors.Join(err, reconciler.reconcileAlerts(ctx, service))
}

// reconcileService keeps model lifecycle and serving readiness independent of optional alert delivery.
func (reconciler *ModelServiceReconciler) reconcileService(ctx context.Context, service *inferencev1alpha1.ModelService) (ctrl.Result, error) {
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

	sourceAllowed, err := reconciler.sourceSelectionAllowed(ctx, service)
	if err != nil {
		return ctrl.Result{}, err
	}
	sourceRevision, err := runtimeconfig.SourceRevision(service.Annotations, sourceAllowed)
	var compiledPools []compiler.ModelPool
	if err == nil {
		compiledPools, err = compiler.CompileModelService(service.Spec)
	}
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
	scaling, err := reconciler.scalingConfig(service)
	var autoscalingStatus []inferencev1alpha1.AutoscalingTargetStatus
	if err == nil {
		compiledPools, autoscalingStatus, err = reconciler.applyScaling(ctx, service, compiledPools, scaling)
	}
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionFalse, "ScalingFailed", err.Error()},
			pools:    conditionState{metav1.ConditionFalse, "ScalingFailed", "ModelPool capacity was not resolved"},
			ready:    conditionState{metav1.ConditionFalse, "ScalingFailed", "ModelService capacity is invalid"},
		})
	}
	runtimeCache, cacheReady, err := reconciler.CacheProfile.Resolve(ctx, reconciler.Client, service.Namespace)
	if err != nil {
		statusErr := reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionTrue, "Compiled", "ModelService intent was compiled"},
			pools:    conditionState{metav1.ConditionFalse, "CacheResolutionFailed", "Runtime cache could not be resolved"},
			ready:    conditionState{metav1.ConditionFalse, "CacheResolutionFailed", err.Error()},
		})
		return ctrl.Result{}, errors.Join(err, statusErr)
	}
	if !cacheReady {
		ready, reason, message, readinessErr := reconciler.serviceReadiness(ctx, service, compiledPools)
		if ready {
			reason, message = "ServingPreviousGeneration", "The previous complete ModelService generation remains ready while runtime cache storage is preparing"
		}
		statusErr := reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionTrue, "Compiled", "ModelService intent was compiled"},
			pools:    conditionState{metav1.ConditionFalse, "CacheNotReady", "No new ModelPools were materialized"},
			ready:    conditionState{conditionStatus(ready), reason, message},
		})
		return ctrl.Result{}, errors.Join(readinessErr, statusErr)
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
		statusErr := reconciler.updateStatus(ctx, service, modelServiceState{
			compiled: conditionState{metav1.ConditionTrue, "Compiled", "ModelService intent was compiled"},
			pools:    conditionState{metav1.ConditionFalse, "ApplyFailed", "ModelPools were not fully materialized"},
			ready:    conditionState{metav1.ConditionFalse, "PoolsNotReady", "ModelPools are not ready"},
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
	pools := conditionState{metav1.ConditionTrue, "Applied", "All ModelPools were materialized"}
	if err := reconciler.updateStatus(ctx, service, modelServiceState{
		compiled:    conditionState{metav1.ConditionTrue, "Compiled", "ModelService intent was compiled"},
		pools:       pools,
		ready:       conditionState{conditionStatus(ready), readyReason, readyMessage},
		autoscaling: &autoscalingStatus,
	}); err != nil {
		return ctrl.Result{}, err
	}
	if scaling.Autoscaler.Automatic() {
		return ctrl.Result{RequeueAfter: scaling.PollingInterval}, nil
	}
	return ctrl.Result{}, nil
}

// sourceSelectionAllowed admits new source intent only in source mode, while preserving
// controller-owned selections after that mode is disabled.
func (reconciler *ModelServiceReconciler) sourceSelectionAllowed(ctx context.Context, service *inferencev1alpha1.ModelService) (bool, error) {
	if reconciler.SourceMode {
		return true, nil
	}
	revision := service.Annotations[runtimeconfig.SourceRevisionAnnotation]
	if revision == "" {
		return false, nil
	}
	if len(service.Status.PoolApplications) > 0 {
		for _, selected := range service.Status.PoolApplications {
			if selected.SourceRevision != revision || selected.DeploymentRevision != service.Spec.DeploymentRevision {
				return false, nil
			}
		}
		return true, nil
	}
	pools, err := reconciler.ownedPools(ctx, service)
	if err != nil {
		return false, err
	}
	if len(pools) == 0 {
		return false, nil
	}
	for _, pool := range pools {
		if pool.Spec.Template.SourceRevision != revision {
			return false, nil
		}
		selected := pool.Spec.Template.Application
		if selected == nil {
			if service.Spec.DeploymentRevision != "" {
				return false, nil
			}
		} else if selected.DeploymentRevision != service.Spec.DeploymentRevision {
			return false, nil
		}
	}
	return true, nil
}

// reconcilePools converges compiled ModelPool contracts and retains service-selected serving pools.
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

	// Cache identity follows encoder content, not replica counts or service alert settings.
	cacheGeneration := service.Generation
	for _, compiled := range compiledPools {
		if compiled.Template.Role != inferencev1alpha1.ModelRoleEncoder {
			continue
		}
		if previous := byPoolName[compiled.Name]; previous != nil {
			before, after := previous.Spec.Template, compiled.Template
			if before.EncoderCacheGeneration > 0 && before.Model == after.Model && before.Source == after.Source && before.ModelRevision == after.ModelRevision && before.Backend == after.Backend && before.ECProfile == after.ECProfile && reflect.DeepEqual(before.EngineArgs, after.EngineArgs) {
				cacheGeneration = before.EncoderCacheGeneration
			}
		}
	}
	base := service.DeepCopy()
	applications := make(map[string]inferencev1alpha1.ApplicationSelection, len(compiledPools))
	for index := range compiledPools {
		template := &compiledPools[index].Template
		previous := byPoolName[compiledPools[index].Name]
		selection, err := reconciler.selectPoolApplication(ctx, service, compiledPools[index].Name, compiledPools[index].DesiredGroups, previous, *template)
		if err != nil {
			return err
		}
		template.Application = selection
		if selection != nil {
			applications[compiledPools[index].Name] = *selection
		}
		if compiledPools[index].Template.ECProfile != "" {
			compiledPools[index].Template.EncoderCacheGeneration = cacheGeneration
		}
	}

	if len(applications) == 0 {
		applications = nil
	}
	if !reflect.DeepEqual(service.Status.PoolApplications, applications) {
		service.Status.PoolApplications = applications
		if err := reconciler.Status().Patch(ctx, service, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return fmt.Errorf("persist Pool application selections: %w", err)
		}
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
		if _, keep := desired[pool.Spec.PoolName]; keep || serviceServingRevision(service, pool) != "" {
			continue
		}
		if err := reconciler.Delete(ctx, pool); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("delete stale ModelPool %q: %w", pool.Name, err)
		}
	}
	return nil
}

// selectPoolApplication retains execution choices independently of changing platform defaults.
// Existing image-only Pools adopt their actual cohort, never a newly configured file publication.
func (reconciler *ModelServiceReconciler) selectPoolApplication(ctx context.Context, service *inferencev1alpha1.ModelService, poolName string, desiredGroups int32, previous *inferencev1alpha1.ModelPool, template inferencev1alpha1.NormalizedPoolTemplate) (*inferencev1alpha1.ApplicationSelection, error) {
	deployment := service.Spec.DeploymentRevision
	image := reconciler.RuntimeProfile.Image
	imageProfile := template.Backend
	if template.Backend == "vllm-omni" {
		image = reconciler.RuntimeProfile.OmniImage
	} else if template.Profiling != nil && template.Profiling.Engine == "nsight" {
		image = reconciler.RuntimeProfile.NsightImage
		imageProfile = "nsight"
	}
	matches := func(selected inferencev1alpha1.ApplicationSelection) bool {
		return selected.DeploymentRevision == deployment && selected.SourceRevision == template.SourceRevision && selected.ImageProfile == imageProfile
	}
	if selected, exists := service.Status.PoolApplications[poolName]; exists && matches(selected) {
		return selected.DeepCopy(), nil
	}
	if previous == nil && template.SourceRevision != "" && !reconciler.SourceMode {
		for _, selected := range service.Status.PoolApplications {
			if matches(selected) {
				return selected.DeepCopy(), nil
			}
		}
		pools, err := reconciler.ownedPools(ctx, service)
		if err != nil {
			return nil, err
		}
		for index := range pools {
			if pools[index].Spec.Template.SourceRevision == template.SourceRevision && pools[index].Spec.Template.Backend == template.Backend {
				previous = &pools[index]
				break
			}
		}
	}
	selection := &inferencev1alpha1.ApplicationSelection{Image: image, ImageProfile: imageProfile, SourceRevision: template.SourceRevision, DeploymentRevision: deployment}
	if previous != nil && previous.Spec.Template.Backend == template.Backend && previous.Spec.Template.SourceRevision == template.SourceRevision {
		previousProfile := previous.Spec.Template.Backend
		if previous.Spec.Template.Profiling != nil && previous.Spec.Template.Profiling.Engine == "nsight" {
			previousProfile = "nsight"
		}
		if selected := previous.Spec.Template.Application; selected != nil && selected.DeploymentRevision == deployment && previousProfile == imageProfile {
			retained := selected.DeepCopy()
			retained.SourceRevision, retained.ImageProfile = template.SourceRevision, imageProfile
			return retained, nil
		}
		if previous.Spec.Template.Application == nil && deployment == "" && previousProfile == imageProfile {
			groups, err := ownedModelGroups(ctx, reconciler.Client, previous)
			if err != nil {
				return nil, err
			}
			for _, group := range groups {
				if len(groups) == 1 || group.Spec.Revision == previous.Status.PreparedRevision || group.Spec.Revision == serviceServingRevision(service, previous) {
					selection.Image = group.Spec.Runtime.Image
					selection.ApplicationURL = group.Spec.Runtime.ApplicationURL
					if selection.ApplicationURL == "" {
						selection.ApplicationURL = reconciler.ApplicationFiles.Ref("model-server", group.Spec.Runtime.SourceRevision)
					}
					return selection, nil
				}
			}
			// An idle legacy Pool with no execution history has nothing to recover.
			// Keep it unselected until its first demand for capacity chooses the current pair.
			if desiredGroups == 0 {
				return nil, nil
			}
		}
	}
	if template.Backend == "vllm" {
		selection.ApplicationURL = reconciler.ApplicationURL
		if template.SourceRevision != "" {
			selection.ApplicationURL = reconciler.ApplicationFiles.Ref("model-server", template.SourceRevision)
		}
	}
	return selection, nil
}

// commitServingGeneration atomically selects only fully prepared ModelPool revisions for frontend routing.
func (reconciler *ModelServiceReconciler) commitServingGeneration(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) (bool, error) {
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
		if compiled.DesiredGroups == 0 {
			continue
		}
		pool := byName[compiled.Name]
		if pool == nil || pool.Spec.DesiredGroups != compiled.DesiredGroups || !reflect.DeepEqual(pool.Spec.Template, compiled.Template) || pool.Status.ObservedGeneration != pool.Generation || pool.Status.PreparedRevision == "" || !poolRevisionReady(groups.Items, pool, pool.Status.PreparedRevision, pool.Spec.DesiredGroups) {
			return false, nil
		}
		selected = append(selected, inferencev1alpha1.ServingPoolRevision{PoolName: pool.Spec.PoolName, PoolUID: string(pool.UID), Revision: pool.Status.PreparedRevision})
	}
	slices.SortFunc(selected, func(left, right inferencev1alpha1.ServingPoolRevision) int {
		return compareStrings(left.PoolName, right.PoolName)
	})
	candidate := service.DeepCopy()
	candidate.Status.ServingPoolRevisions = selected
	servicePools := ownedRoutingPools(candidate, pools)
	servicePools = slices.DeleteFunc(servicePools, func(pool *inferencev1alpha1.ModelPool) bool {
		return serviceServingRevision(candidate, pool) == ""
	})
	if len(selected) > 0 {
		switch {
		case poolsHaveEPD(servicePools) || serviceDeclaresEPD(candidate):
			if !poolsContainCompleteEPD(servicePools) {
				return false, nil
			}
			if _, _, err := projectServiceEPDComponents(candidate, servicePools, groups.Items); err != nil {
				return false, err
			}
		case poolsHavePD(servicePools):
			if _, _, err := projectServicePDComponents(candidate, servicePools, groups.Items); err != nil {
				return false, err
			}
		default:
			routes := make([]servingSnapshotGroup, 0)
			for _, pool := range servicePools {
				revision := serviceServingRevision(candidate, pool)
				for index := range groups.Items {
					group := &groups.Items[index]
					if routingGroupOwnedBy(group, pool) && group.Spec.Revision == revision && routingGroupReady(group) && group.Spec.Role == inferencev1alpha1.ModelRoleAggregate {
						routes = append(routes, routingGroupForService(candidate, pool, group))
					}
				}
			}
			if err := validateRoutingIdentities(routes, nil, nil); err != nil {
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

// serviceReadiness verifies that the selected serving generation remains routable.
func (reconciler *ModelServiceReconciler) serviceReadiness(ctx context.Context, service *inferencev1alpha1.ModelService, compiledPools []compiler.ModelPool) (bool, string, string, error) {
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
		if pool == nil || string(pool.UID) != selected.PoolUID || serviceServingRevision(service, pool) != selected.Revision {
			return false, "PoolsNotReady", "One or more serving ModelPool revisions are unavailable", nil
		}
		selectedPools = append(selectedPools, pool)
	}
	var groups inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &groups, client.InNamespace(service.Namespace)); err != nil {
		return false, "PoolsNotReady", "ModelGroups are not ready", err
	}
	switch {
	case poolsHaveEPD(selectedPools):
		if _, _, err := projectServiceEPDComponents(service, selectedPools, groups.Items); err != nil {
			return false, "PoolsNotReady", "The serving E/P/D generation is incomplete", nil
		}
	case poolsHavePD(selectedPools):
		if _, _, err := projectServicePDComponents(service, selectedPools, groups.Items); err != nil {
			return false, "PoolsNotReady", "The serving P/D generation is incomplete", nil
		}
	default:
		for _, pool := range selectedPools {
			if !poolRevisionServingReady(groups.Items, pool, serviceServingRevision(service, pool)) {
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
		if !routingGroupOwnedBy(group, pool) || group.Spec.Revision != revision || group.Spec.Ordinal >= desired || !routingGroupReady(group) {
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
		return routingGroupOwnedBy(&group, pool) && group.Spec.Revision == revision && routingGroupReady(&group)
	})
}

func serviceServingRevision(service *inferencev1alpha1.ModelService, pool *inferencev1alpha1.ModelPool) string {
	if service == nil || pool == nil {
		return ""
	}
	for _, selected := range service.Status.ServingPoolRevisions {
		if selected.PoolName == pool.Spec.PoolName && selected.PoolUID == string(pool.UID) {
			return selected.Revision
		}
	}
	return ""
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

// reconcileDelete removes owned ModelPools before releasing the ModelService finalizer.
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

// ownedPools returns ModelPools whose reference and controller owner both identify the ModelService.
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

// updateStatus publishes compilation, pool, readiness, and autoscaling results for the ModelService.
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
// current binding configuration before any ModelPool is created or changed.
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
func (reconciler *ModelServiceReconciler) modelServicesForGroup(ctx context.Context, object client.Object) []reconcile.Request {
	group := object.(*inferencev1alpha1.ModelGroup)
	pool := new(inferencev1alpha1.ModelPool)
	if err := reconciler.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: group.Spec.ModelPoolRef.Name}, pool); err != nil {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: pool.Namespace, Name: pool.Spec.ModelServiceRef.Name}}}
}

// modelServicesInNamespace maps shared platform resource changes to every ModelService in the namespace.
func (reconciler *ModelServiceReconciler) modelServicesInNamespace(ctx context.Context, object client.Object) []reconcile.Request {
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
