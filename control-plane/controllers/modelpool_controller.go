// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles ModelPool resources into controller-owned ModelGroups.

package controllers

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"reflect"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
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
		Complete(reconciler)
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
	if err := reconciler.validateModelServiceOwnership(ctx, pool); err != nil {
		return ctrl.Result{}, err
	}
	if reconciler.TemplateResolver == nil {
		return ctrl.Result{}, fmt.Errorf("ModelPool template resolver is not configured")
	}
	template, err := reconciler.TemplateResolver.Resolve(pool.Spec.Template)
	if err != nil {
		state, stateErr := reconciler.currentActiveState(ctx, pool)
		state.Reason, state.Message = "ResolutionFailed", "The target Pool execution config could not be resolved"
		statusErr := reconciler.updateStatus(ctx, pool, metav1.ConditionFalse, "ResolutionFailed", err.Error(), state)
		return ctrl.Result{}, errors.Join(stateErr, statusErr)
	}
	state, err := reconciler.reconcileGroups(ctx, pool, template)
	if err != nil {
		active, stateErr := reconciler.currentActiveState(ctx, pool)
		active.Reason, active.Message = "ApplyFailed", "ModelGroups were not fully materialized"
		statusErr := reconciler.updateStatus(ctx, pool, metav1.ConditionTrue, "Resolved", "Pool execution config was resolved", active)
		return ctrl.Result{}, errors.Join(err, stateErr, statusErr)
	}
	if err := reconciler.updateStatus(ctx, pool, metav1.ConditionTrue, "Resolved", "Pool execution config was resolved", state); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

func (reconciler *ModelPoolReconciler) validateModelServiceOwnership(ctx context.Context, pool *inferencev1alpha1.ModelPool) error {
	service := new(inferencev1alpha1.ModelService)
	key := client.ObjectKey{Namespace: pool.Namespace, Name: pool.Spec.ModelServiceRef.Name}
	if err := reconciler.Get(ctx, key, service); err != nil {
		return fmt.Errorf("get owning ModelService: %w", err)
	}
	if pool.Spec.ModelServiceRef.UID != string(service.UID) || !metav1.IsControlledBy(pool, service) {
		return fmt.Errorf("ModelPool %q is not owned by its referenced ModelService", pool.Name)
	}
	return nil
}

type groupState struct {
	Materialized         bool
	Ready                bool
	CapacityReady        bool
	RolloutPending       bool
	InsufficientCapacity bool
	ActiveRevision       string
	Reason               string
	Message              string
}

func (reconciler *ModelPoolReconciler) currentActiveState(ctx context.Context, pool *inferencev1alpha1.ModelPool) (groupState, error) {
	groups, err := reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return groupState{ActiveRevision: pool.Status.ActiveRevision}, err
	}
	active := pool.Status.ActiveRevision
	return groupState{
		Ready:          revisionServingReady(groups, active),
		RolloutPending: active != "",
		ActiveRevision: active,
	}, nil
}

func (reconciler *ModelPoolReconciler) reconcileGroups(ctx context.Context, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate) (groupState, error) {
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
	activeRevision := pool.Status.ActiveRevision
	rolloutPending := (activeRevision != "" && activeRevision != template.Revision) || !targetReady
	ready := revisionServingReady(groups, activeRevision)
	if activeRevision != template.Revision && targetReady {
		activeRevision = template.Revision
		ready = true
		rolloutPending = true
	}

	if pool.Spec.DesiredGroups == 0 {
		activeRevision = ""
		ready = false
	}

	// Retire a previous revision only after the target revision was published as
	// active in an earlier status update. Routing therefore switches before delete.
	if pool.Status.ActiveRevision == template.Revision || pool.Spec.DesiredGroups == 0 {
		for index := range groups {
			group := &groups[index]
			if group.Spec.Revision == template.Revision && pool.Spec.DesiredGroups > 0 {
				continue
			}
			rolloutPending = true
			if err := reconciler.Delete(ctx, group); err != nil && !apierrors.IsNotFound(err) {
				return groupState{}, fmt.Errorf("delete superseded ModelGroup %q: %w", group.Name, err)
			}
		}
	}

	state := groupState{
		Materialized:         materialized,
		Ready:                ready && pool.Spec.DesiredGroups > 0,
		CapacityReady:        targetReady,
		RolloutPending:       rolloutPending || scalePending,
		InsufficientCapacity: !targetReady && targetInsufficientCapacity,
		ActiveRevision:       activeRevision,
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

func revisionReady(groups []inferencev1alpha1.ModelGroup, revision string, desired int32) bool {
	if revision == "" || desired == 0 {
		return false
	}
	current := make(map[int32]*inferencev1alpha1.ModelGroup, desired)
	for index := range groups {
		group := &groups[index]
		if group.Spec.Revision != revision || group.Spec.Ordinal >= desired {
			continue
		}
		if current[group.Spec.Ordinal] != nil {
			return false
		}
		current[group.Spec.Ordinal] = group
	}
	return groupsReady(current, desired)
}

// revisionServingReady reports whether an active revision still has a routable Group.
// Desired capacity converges separately through revisionReady.
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
	prefix := strings.ReplaceAll(poolName, ".", "-")
	if prefix != poolName {
		hash := fmt.Sprintf("-%x", sha256.Sum256([]byte(poolName)))[:9]
		maxPrefix := 63 - len(suffix) - len(hash)
		if len(prefix) > maxPrefix {
			prefix = strings.TrimRight(prefix[:maxPrefix], "-")
		}
		prefix += hash
	} else if len(prefix)+len(suffix) > 63 {
		prefix = strings.TrimRight(prefix[:63-len(suffix)], "-")
	}
	group.Name = prefix + suffix
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
	pool.Status.ActiveRevision = state.ActiveRevision
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
		return "ActiveRevisionReady", "The active revision remains ready while requested capacity is converging"
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
