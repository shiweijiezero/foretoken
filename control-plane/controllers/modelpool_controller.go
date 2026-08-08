// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
//
// Reconciles ModelPool resources into immutable, ordinal ModelGroups.

package controllers

import (
	"context"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const (
	conditionGroupsMaterialized = "GroupsMaterialized"
	conditionPoolReady          = "Ready"
)

// ModelPoolReconciler owns the immutable ModelGroups created for one ModelPool.
type ModelPoolReconciler struct {
	client.Client
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

// Reconcile creates a target revision before publishing it, then retires the old revision.
func (reconciler *ModelPoolReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	pool := new(inferencev1alpha1.ModelPool)
	if err := reconciler.Get(ctx, request.NamespacedName, pool); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}

	groups, err := reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return ctrl.Result{}, err
	}
	targetRevision := reconciler.targetRevision(pool, groups)
	if err := reconciler.reconcileGroups(ctx, pool, groups, targetRevision); err != nil {
		return ctrl.Result{}, err
	}

	// Re-list after changes so readiness only reflects objects that actually exist.
	groups, err = reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return ctrl.Result{}, err
	}
	targetReady := revisionReady(groups, targetRevision, pool.Spec.DesiredGroups)
	activeRevision := pool.Status.ActiveRevision

	// Cutover is deliberately a separate reconciliation step: this status write publishes
	// the ready target before the next pass deletes previously active Groups.
	if activeRevision != targetRevision && targetReady {
		activeRevision = targetRevision
	} else if activeRevision == targetRevision {
		if err := reconciler.deleteSupersededGroups(ctx, pool, groups, targetRevision); err != nil {
			return ctrl.Result{}, err
		}
	}

	groups, err = reconciler.ownedGroups(ctx, pool)
	if err != nil {
		return ctrl.Result{}, err
	}
	activeReady := revisionReady(groups, activeRevision, pool.Spec.DesiredGroups)
	return ctrl.Result{}, reconciler.updateStatus(ctx, pool, activeRevision, targetRevision, targetReady, activeReady)
}

// targetRevision keeps the active revision while its Group specs still match the Pool.
func (reconciler *ModelPoolReconciler) targetRevision(pool *inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup) string {
	active := pool.Status.ActiveRevision
	if active != "" {
		for index := range groups {
			group := &groups[index]
			if group.Spec.Revision == active && groupMatchesPool(group, pool, active) {
				return active
			}
		}
	}
	return fmt.Sprintf("revision-%d", pool.Generation)
}

// reconcileGroups creates missing target ordinals and removes ordinals above desired capacity.
func (reconciler *ModelPoolReconciler) reconcileGroups(ctx context.Context, pool *inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, revision string) error {
	current := make(map[int32]*inferencev1alpha1.ModelGroup, len(groups))
	for index := range groups {
		group := &groups[index]
		if group.Spec.Ordinal >= pool.Spec.DesiredGroups {
			if err := reconciler.Delete(ctx, group); err != nil && !apierrors.IsNotFound(err) {
				return fmt.Errorf("delete surplus ModelGroup %q: %w", group.Name, err)
			}
			continue
		}
		if group.Spec.Revision != revision {
			continue
		}
		if existing := current[group.Spec.Ordinal]; existing != nil {
			return fmt.Errorf("ModelPool owns duplicate ModelGroups for revision %q ordinal %d", revision, group.Spec.Ordinal)
		}
		current[group.Spec.Ordinal] = group
	}

	for ordinal := int32(0); ordinal < pool.Spec.DesiredGroups; ordinal++ {
		if group := current[ordinal]; group != nil {
			if !groupMatchesPool(group, pool, revision) {
				return fmt.Errorf("ModelGroup %q conflicts with target revision %q ordinal %d", group.Name, revision, ordinal)
			}
			continue
		}
		group := &inferencev1alpha1.ModelGroup{
			ObjectMeta: metav1.ObjectMeta{
				Namespace: pool.Namespace,
				Name:      modelGroupName(pool.Name, revision, ordinal),
			},
			Spec: modelGroupSpec(pool, revision, ordinal),
		}
		if err := controllerutil.SetControllerReference(pool, group, reconciler.Scheme()); err != nil {
			return fmt.Errorf("set ModelGroup %q owner: %w", group.Name, err)
		}
		if err := reconciler.Create(ctx, group); err != nil {
			return fmt.Errorf("create ModelGroup ordinal %d: %w", ordinal, err)
		}
	}
	return nil
}

// deleteSupersededGroups retires old revisions only after the target revision is active.
func (reconciler *ModelPoolReconciler) deleteSupersededGroups(ctx context.Context, pool *inferencev1alpha1.ModelPool, groups []inferencev1alpha1.ModelGroup, activeRevision string) error {
	for index := range groups {
		group := &groups[index]
		if group.Spec.Revision == activeRevision && group.Spec.Ordinal < pool.Spec.DesiredGroups {
			continue
		}
		if err := reconciler.Delete(ctx, group); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("delete superseded ModelGroup %q: %w", group.Name, err)
		}
	}
	return nil
}

// ownedGroups accepts Groups only when their stable reference and owner reference agree.
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

// updateStatus publishes the active revision and the target and serving readiness conditions.
func (reconciler *ModelPoolReconciler) updateStatus(ctx context.Context, pool *inferencev1alpha1.ModelPool, activeRevision, targetRevision string, targetReady, activeReady bool) error {
	base := pool.DeepCopy()
	pool.Status.ObservedGeneration = pool.Generation
	pool.Status.ActiveRevision = activeRevision
	phase := inferencev1alpha1.ModelPoolPhaseProgressing
	if activeReady && activeRevision == targetRevision {
		phase = inferencev1alpha1.ModelPoolPhaseReady
	}
	pool.Status.Phase = phase
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{
		Type:               conditionGroupsMaterialized,
		Status:             boolCondition(targetReady),
		Reason:             materializedReason(targetReady),
		Message:            materializedMessage(targetReady),
		ObservedGeneration: pool.Generation,
	})
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{
		Type:               conditionPoolReady,
		Status:             boolCondition(activeReady),
		Reason:             readyReason(activeReady, activeRevision, targetRevision),
		Message:            readyMessage(activeReady, activeRevision, targetRevision),
		ObservedGeneration: pool.Generation,
	})
	if reflect.DeepEqual(base.Status, pool.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, pool, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelPool status: %w", err)
	}
	return nil
}

// modelGroupName preserves the revision and ordinal suffix within the DNS label limit.
func modelGroupName(poolName, revision string, ordinal int32) string {
	suffix := fmt.Sprintf("-%s-%d", revision, ordinal)
	if len(poolName)+len(suffix) <= 63 {
		return poolName + suffix
	}
	return poolName[:63-len(suffix)] + suffix
}

// modelGroupSpec projects the normalized Pool template into one immutable Group contract.
func modelGroupSpec(pool *inferencev1alpha1.ModelPool, revision string, ordinal int32) inferencev1alpha1.ModelGroupSpec {
	template := pool.Spec.Template
	return inferencev1alpha1.ModelGroupSpec{
		ModelPoolRef: inferencev1alpha1.LocalObjectReference{Name: pool.Name, UID: string(pool.UID)},
		Revision:     revision,
		Ordinal:      ordinal,
		Role:         template.Role,
		NodeCount:    template.NodeCount,
		MemberCount:  template.MemberCount,
		Parallelism:  template.Parallelism,
		Accelerator: inferencev1alpha1.ModelGroupAccelerator{
			Type:           template.Resources.Requests.GPU.Type,
			CountPerMember: template.Resources.Requests.GPU.Count,
		},
		Network: template.Network,
	}
}

// groupMatchesPool checks whether an immutable Group still realizes the current Pool template.
func groupMatchesPool(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool, revision string) bool {
	desired := modelGroupSpec(pool, revision, group.Spec.Ordinal)
	return reflect.DeepEqual(group.Spec, desired)
}

// revisionReady requires every desired ordinal for a revision to be fully ready.
func revisionReady(groups []inferencev1alpha1.ModelGroup, revision string, desired int32) bool {
	if revision == "" {
		return desired == 0
	}
	byOrdinal := make(map[int32]*inferencev1alpha1.ModelGroup, desired)
	for index := range groups {
		group := &groups[index]
		if group.Spec.Revision != revision || group.Spec.Ordinal >= desired {
			continue
		}
		if byOrdinal[group.Spec.Ordinal] != nil {
			return false
		}
		byOrdinal[group.Spec.Ordinal] = group
	}
	for ordinal := int32(0); ordinal < desired; ordinal++ {
		group := byOrdinal[ordinal]
		if group == nil || group.Status.Phase != inferencev1alpha1.ModelGroupPhaseReady || group.Status.ReadyMembers != group.Spec.MemberCount || group.Status.TotalMembers != group.Spec.MemberCount {
			return false
		}
	}
	return true
}

func boolCondition(value bool) metav1.ConditionStatus {
	if value {
		return metav1.ConditionTrue
	}
	return metav1.ConditionFalse
}

func materializedReason(ready bool) string {
	if ready {
		return "TargetRevisionReady"
	}
	return "TargetRevisionPending"
}

func materializedMessage(ready bool) string {
	if ready {
		return "Every desired ordinal in the target revision is ready"
	}
	return "Waiting for every desired ordinal in the target revision to become ready"
}

func readyReason(ready bool, activeRevision, targetRevision string) string {
	if ready && activeRevision != targetRevision {
		return "ActiveRevisionReady"
	}
	if ready {
		return "RevisionReady"
	}
	return "ActiveRevisionPending"
}

func readyMessage(ready bool, activeRevision, targetRevision string) string {
	if ready && activeRevision != targetRevision {
		return "The active revision remains ready while the target revision is pending"
	}
	if ready {
		return "Every desired ordinal in the active revision is ready"
	}
	return "Waiting for every desired ordinal in the active revision to become ready"
}
