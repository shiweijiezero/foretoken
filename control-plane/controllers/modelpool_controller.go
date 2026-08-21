// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles ModelPool resources into controller-owned ModelGroups.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	conditionResolved           = "Resolved"
	conditionGroupsMaterialized = "GroupsMaterialized"
	conditionRolloutPending     = "RolloutPending"
)

// ModelPoolTemplateResolver selects platform runtime settings and resolves one Group template.
type ModelPoolTemplateResolver interface {
	Resolve(inferencev1alpha1.NormalizedPoolTemplate) (resolver.ModelGroupTemplate, error)
}

// ModelPoolReconciler resolves Pool templates and owns ModelGroup specs.
type ModelPoolReconciler struct {
	client.Client
	TemplateResolver ModelPoolTemplateResolver
}

// SetupWithManager registers the ModelPool controller and its owned Groups.
func (reconciler *ModelPoolReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.ModelPool{}).
		Owns(&inferencev1alpha1.ModelGroup{}).
		Watches(&inferencev1alpha1.ModelService{}, handler.EnqueueRequestsFromMapFunc(reconciler.poolsForService)).
		Complete(reconciler)
}

func (reconciler *ModelPoolReconciler) poolsForService(ctx context.Context, object client.Object) []reconcile.Request {
	var pools inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(object.GetNamespace())); err != nil {
		return nil
	}
	requests := make([]reconcile.Request, 0)
	for index := range pools.Items {
		pool := &pools.Items[index]
		if pool.Spec.ModelServiceRef.Name == object.GetName() && pool.Spec.ModelServiceRef.UID == string(object.GetUID()) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(pool)})
		}
	}
	return requests
}

// +kubebuilder:rbac:groups=inference.foretoken.io,resources=modelpools,verbs=get;list;watch
// +kubebuilder:rbac:groups=inference.foretoken.io,resources=modelpools/status,verbs=get;patch;update
// +kubebuilder:rbac:groups=inference.foretoken.io,resources=modelgroups,verbs=get;list;watch;create;delete

// Reconcile materializes the desired Group revision and aggregates Group readiness.
func (reconciler *ModelPoolReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	pool := new(inferencev1alpha1.ModelPool)
	if err := reconciler.Get(ctx, request.NamespacedName, pool); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !pool.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}
	service, err := reconciler.validateModelServiceOwnership(ctx, pool)
	if err != nil {
		return ctrl.Result{}, err
	}
	if reconciler.TemplateResolver == nil {
		return ctrl.Result{}, fmt.Errorf("ModelPool template resolver is not configured")
	}
	template, err := reconciler.TemplateResolver.Resolve(pool.Spec.Template)
	if err != nil {
		state, stateErr := reconciler.currentActiveState(ctx, pool, serviceServingRevision(service, pool))
		state.Reason, state.Message = "ResolutionFailed", "The target Pool execution config could not be resolved"
		statusErr := reconciler.updateStatus(ctx, pool, metav1.ConditionFalse, "ResolutionFailed", err.Error(), state)
		return ctrl.Result{}, errors.Join(stateErr, statusErr)
	}
	servingRevision := serviceServingRevision(service, pool)
	template.Revision, err = reconciler.targetRevision(ctx, pool, template, servingRevision)
	if err != nil {
		return ctrl.Result{}, err
	}
	state, err := reconciler.reconcileGroups(ctx, pool, template, servingRevision)
	if err != nil {
		active, stateErr := reconciler.currentActiveState(ctx, pool, serviceServingRevision(service, pool))
		active.Reason, active.Message = "ApplyFailed", "ModelGroups were not fully materialized"
		statusErr := reconciler.updateStatus(ctx, pool, metav1.ConditionTrue, "Resolved", "Pool execution config was resolved", active)
		return ctrl.Result{}, errors.Join(err, stateErr, statusErr)
	}
	if err := reconciler.updateStatus(ctx, pool, metav1.ConditionTrue, "Resolved", "Pool execution config was resolved", state); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (reconciler *ModelPoolReconciler) validateModelServiceOwnership(ctx context.Context, pool *inferencev1alpha1.ModelPool) (*inferencev1alpha1.ModelService, error) {
	service := new(inferencev1alpha1.ModelService)
	key := client.ObjectKey{Namespace: pool.Namespace, Name: pool.Spec.ModelServiceRef.Name}
	if err := reconciler.Get(ctx, key, service); err != nil {
		return nil, fmt.Errorf("get owning ModelService: %w", err)
	}
	if pool.Spec.ModelServiceRef.UID != string(service.UID) || !metav1.IsControlledBy(pool, service) {
		return nil, fmt.Errorf("ModelPool %q is not owned by its referenced ModelService", pool.Name)
	}
	return service, nil
}

func (reconciler *ModelPoolReconciler) targetRevision(ctx context.Context, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate, servingRevision string) (string, error) {
	// Reuse a prepared or serving cohort when its immutable template still matches. This lets
	// retries and pure scale changes converge without minting a disruptive new revision.
	groups, err := reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return "", err
	}
	for _, revision := range []string{pool.Status.PreparedRevision, servingRevision} {
		if revision == "" {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if group.Spec.Revision == revision && groupMatchesTemplate(group, pool, template) {
				return revision, nil
			}
		}
	}
	return fmt.Sprintf("revision-%d-%s", pool.Generation, template.Revision), nil
}

func groupMatchesTemplate(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate) bool {
	template.Revision = group.Spec.Revision
	return reflect.DeepEqual(group.Spec, template.Spec(pool, group.Spec.Ordinal))
}

type groupState struct {
	Materialized         bool
	Ready                bool
	CapacityReady        bool
	RolloutPending       bool
	InsufficientCapacity bool
	PreparedRevision     string
	Reason               string
	Message              string
}

func (reconciler *ModelPoolReconciler) currentActiveState(ctx context.Context, pool *inferencev1alpha1.ModelPool, servingRevision string) (groupState, error) {
	groups, err := reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return groupState{PreparedRevision: pool.Status.PreparedRevision}, err
	}
	return groupState{
		Ready:            revisionServingReady(groups, servingRevision),
		RolloutPending:   pool.Status.PreparedRevision != servingRevision,
		PreparedRevision: pool.Status.PreparedRevision,
	}, nil
}

func (reconciler *ModelPoolReconciler) reconcileGroups(ctx context.Context, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate, servingRevision string) (groupState, error) {
	groups, err := reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return groupState{}, err
	}
	current := make(map[int32]*inferencev1alpha1.ModelGroup, len(groups))
	for index := range groups {
		group := &groups[index]
		if group.Spec.Revision != template.Revision {
			continue
		}
		if previous := current[group.Spec.Ordinal]; previous != nil {
			return groupState{}, fmt.Errorf("ModelPool owns duplicate ModelGroups for revision %q ordinal %d", template.Revision, group.Spec.Ordinal)
		}
		current[group.Spec.Ordinal] = group
	}

	scalePending := false
	for ordinal, group := range current {
		if ordinal < pool.Spec.DesiredGroups {
			continue
		}
		scalePending = true
		if err := reconciler.Delete(ctx, group); err != nil && !apierrors.IsNotFound(err) {
			return groupState{}, fmt.Errorf("delete excess ModelGroup %q: %w", group.Name, err)
		}
		delete(current, ordinal)
	}

	for ordinal := int32(0); ordinal < pool.Spec.DesiredGroups; ordinal++ {
		spec := template.Spec(pool, ordinal)
		if existing := current[ordinal]; existing != nil {
			if !reflect.DeepEqual(existing.Spec, spec) {
				return groupState{}, fmt.Errorf("ModelGroup %q has an unexpected immutable spec", existing.Name)
			}
			continue
		}
		group := &inferencev1alpha1.ModelGroup{ObjectMeta: metav1.ObjectMeta{Namespace: pool.Namespace}, Spec: spec}
		setGroupName(group, pool.Name, template.Revision, ordinal)
		if err := controllerutil.SetControllerReference(pool, group, reconciler.Scheme()); err != nil {
			return groupState{}, fmt.Errorf("set ModelGroup owner: %w", err)
		}
		if err := reconciler.Create(ctx, group); err != nil {
			return groupState{}, fmt.Errorf("create ModelGroup ordinal %d: %w", ordinal, err)
		}
		current[ordinal] = group
	}

	materialized := int32(len(current)) == pool.Spec.DesiredGroups
	targetReady := materialized && pool.Spec.DesiredGroups > 0 && groupsReady(current, pool.Spec.DesiredGroups)
	targetInsufficientCapacity := materialized && groupsInsufficientCapacity(current, pool.Spec.DesiredGroups)
	preparedRevision := pool.Status.PreparedRevision
	if targetReady {
		preparedRevision = template.Revision
	} else if preparedRevision == template.Revision {
		preparedRevision = ""
	}
	if pool.Spec.DesiredGroups == 0 {
		preparedRevision = ""
	}
	ready := pool.Spec.DesiredGroups > 0 && revisionServingReady(groups, servingRevision)
	rolloutPending := preparedRevision != template.Revision || servingRevision != template.Revision || !targetReady

	// The Pool keeps both its target cohort and the service-selected serving cohort.
	// Other revisions are no longer reachable and can enter their normal drain finalizer.
	for index := range groups {
		group := &groups[index]
		if (pool.Spec.DesiredGroups > 0 && group.Spec.Revision == template.Revision) || group.Spec.Revision == servingRevision {
			continue
		}
		rolloutPending = true
		if err := reconciler.Delete(ctx, group); err != nil && !apierrors.IsNotFound(err) {
			return groupState{}, fmt.Errorf("delete superseded ModelGroup %q: %w", group.Name, err)
		}
	}

	state := groupState{
		Materialized:         materialized,
		Ready:                ready,
		CapacityReady:        targetReady,
		RolloutPending:       rolloutPending || scalePending,
		InsufficientCapacity: !targetReady && targetInsufficientCapacity,
		PreparedRevision:     preparedRevision,
		Reason:               "Applied",
		Message:              "All requested ModelGroups were materialized",
	}
	if state.RolloutPending {
		state.Reason = "RolloutPending"
		state.Message = "Requested Group capacity is converging or superseded Groups are being retired"
	}
	return state, nil
}

func groupsReady(groups map[int32]*inferencev1alpha1.ModelGroup, desired int32) bool {
	for ordinal := int32(0); ordinal < desired; ordinal++ {
		if !modelGroupReady(groups[ordinal]) {
			return false
		}
	}
	return true
}

func groupsInsufficientCapacity(groups map[int32]*inferencev1alpha1.ModelGroup, desired int32) bool {
	for ordinal := int32(0); ordinal < desired; ordinal++ {
		group := groups[ordinal]
		if group == nil {
			continue
		}
		condition := meta.FindStatusCondition(group.Status.Conditions, conditionSchedulingCapacity)
		if condition != nil && condition.Status == metav1.ConditionFalse && condition.Reason == "InsufficientCapacity" && condition.ObservedGeneration == group.Generation {
			return true
		}
	}
	return false
}

// revisionServingReady reports whether an active revision still has a routable Group.
func revisionServingReady(groups []inferencev1alpha1.ModelGroup, revision string) bool {
	if revision == "" {
		return false
	}
	for index := range groups {
		group := &groups[index]
		if group.Spec.Revision == revision && modelGroupReady(group) {
			return true
		}
	}
	return false
}

func modelGroupReady(group *inferencev1alpha1.ModelGroup) bool {
	if group == nil || !group.DeletionTimestamp.IsZero() {
		return false
	}
	condition := meta.FindStatusCondition(group.Status.Conditions, conditionReady)
	return condition != nil &&
		condition.Status == metav1.ConditionTrue &&
		condition.ObservedGeneration == group.Generation
}

func setGroupName(group *inferencev1alpha1.ModelGroup, poolName, revision string, ordinal int32) {
	suffix := fmt.Sprintf("-%s-%d", revision, ordinal)
	if len(poolName)+len(suffix) > 63 {
		poolName = poolName[:63-len(suffix)]
	}
	group.Name = poolName + suffix
}

func (reconciler *ModelPoolReconciler) ownedGroups(ctx context.Context, pool *inferencev1alpha1.ModelPool) ([]inferencev1alpha1.ModelGroup, error) {
	var list inferencev1alpha1.ModelGroupList
	if err := reconciler.List(ctx, &list, client.InNamespace(pool.Namespace)); err != nil {
		return nil, fmt.Errorf("list ModelGroups: %w", err)
	}
	owned := make([]inferencev1alpha1.ModelGroup, 0, len(list.Items))
	for index := range list.Items {
		group := &list.Items[index]
		referenceMatches := group.Spec.ModelPoolRef.Name == pool.Name && group.Spec.ModelPoolRef.UID == string(pool.UID)
		ownerMatches := metav1.IsControlledBy(group, pool)
		if referenceMatches != ownerMatches {
			return nil, fmt.Errorf("ModelGroup %q has inconsistent ModelPool ownership", group.Name)
		}
		if referenceMatches {
			owned = append(owned, *group)
		}
	}
	return owned, nil
}

func (reconciler *ModelPoolReconciler) updateStatus(ctx context.Context, pool *inferencev1alpha1.ModelPool, resolvedStatus metav1.ConditionStatus, resolvedReason, resolvedMessage string, state groupState) error {
	if state.Reason == "" {
		state.Reason = "NotMaterialized"
		state.Message = "ModelGroups were not materialized"
	}
	base := pool.DeepCopy()
	pool.Status.ObservedGeneration = pool.Generation
	pool.Status.PreparedRevision = state.PreparedRevision
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionResolved, Status: resolvedStatus, Reason: resolvedReason, Message: resolvedMessage, ObservedGeneration: pool.Generation})
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionGroupsMaterialized, Status: conditionStatus(state.Materialized), Reason: state.Reason, Message: state.Message, ObservedGeneration: pool.Generation})
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionRolloutPending, Status: conditionStatus(state.RolloutPending), Reason: rolloutReason(state), Message: rolloutMessage(state), ObservedGeneration: pool.Generation})
	readyReason, readyMessage := poolReadyReasonMessage(pool.Spec.DesiredGroups, state.Ready, state.CapacityReady)
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionReady, Status: conditionStatus(state.Ready), Reason: readyReason, Message: readyMessage, ObservedGeneration: pool.Generation})
	if reflect.DeepEqual(base.Status, pool.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, pool, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelPool status: %w", err)
	}
	return nil
}

func poolReadyReasonMessage(desiredGroups int32, ready, capacityReady bool) (string, string) {
	if ready && !capacityReady {
		return "ServingRevisionReady", "The service-selected revision remains ready while requested capacity is converging"
	}
	if ready {
		return "Ready", "All requested ModelGroups are ready"
	}
	if desiredGroups == 0 {
		return "ScaledToZero", "ModelPool has no requested serving capacity"
	}
	return "GroupsNotReady", "One or more requested ModelGroups are not ready"
}

func conditionStatus(value bool) metav1.ConditionStatus {
	if value {
		return metav1.ConditionTrue
	}
	return metav1.ConditionFalse
}

func rolloutReason(state groupState) string {
	if state.InsufficientCapacity {
		return "InsufficientCapacity"
	}
	if state.RolloutPending {
		return "Converging"
	}
	return "Current"
}

func rolloutMessage(state groupState) string {
	if state.InsufficientCapacity {
		return "The target Group revision is Unschedulable; the active revision remains serving"
	}
	if state.RolloutPending {
		return "Requested Group capacity is converging or superseded Groups are being retired"
	}
	return "No previous Group revision is pending rollout"
}
