// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Owns model replica cohorts, runtime resolution, rollout, and capacity retirement.

package model

import (
	"context"
	"errors"
	"fmt"
	"reflect"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/resolver"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/uuid"
	ctrl "sigs.k8s.io/controller-runtime"
	ctrlbuilder "sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/event"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	modelPoolFinalizer      = "inference.foretoken.io/modelpool-protection"
	conditionResolved       = "Resolved"
	conditionRolloutPending = "RolloutPending"
)

// PoolReconciler resolves Pool templates and owns ModelGroup specs.
type PoolReconciler struct {
	client.Client
	APIReader      client.Reader
	RuntimeProfile resolver.RuntimeProfile
}

type groupState struct {
	Materialized         bool
	Ready                bool
	CapacityReady        bool
	Target               metav1.Condition
	RolloutPending       bool
	InsufficientCapacity bool
	PreparedRevision     string
	Reason               string
	Message              string
}

// Reconcile materializes the desired Group revision and aggregates Group readiness.
func (reconciler *PoolReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	pool := new(inferencev1alpha1.ModelPool)
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	if err := reader.Get(ctx, request.NamespacedName, pool); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !pool.DeletionTimestamp.IsZero() {
		if !controllerutil.ContainsFinalizer(pool, modelPoolFinalizer) {
			return ctrl.Result{}, nil
		}
		pending, err := reconciler.retireExcessGroups(ctx, pool, 0)
		if err != nil {
			return ctrl.Result{}, err
		}
		if pending {
			return ctrl.Result{RequeueAfter: modelGroupDrainPollInterval}, nil
		}
		pending, err = reconciler.reconcilePreparationSources(ctx, pool, nil, nil)
		if err != nil {
			return ctrl.Result{}, err
		}
		if pending {
			return ctrl.Result{RequeueAfter: modelGroupDrainPollInterval}, nil
		}
		base := pool.DeepCopy()
		controllerutil.RemoveFinalizer(pool, modelPoolFinalizer)
		return ctrl.Result{}, reconciler.Patch(ctx, pool, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	}
	service, err := reconciler.validateModelServiceOwnership(ctx, pool)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !controllerutil.ContainsFinalizer(pool, modelPoolFinalizer) {
		base := pool.DeepCopy()
		controllerutil.AddFinalizer(pool, modelPoolFinalizer)
		return ctrl.Result{Requeue: true}, reconciler.Patch(ctx, pool, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	}
	// Retain the source before draining Groups, even if the next engine configuration is invalid.
	if _, err := reconciler.reconcilePreparationSources(ctx, pool, service, nil); err != nil {
		return ctrl.Result{}, err
	}
	// Capacity retirement uses the immutable Groups already running. It does not
	// depend on resolving the next template or applying new admission settings.
	retiring, err := reconciler.retireExcessGroups(ctx, pool, pool.Spec.DesiredGroups)
	if err != nil {
		return ctrl.Result{}, err
	}
	if pool.Spec.DesiredGroups == 0 {
		stopped := !retiring
		target := metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Stopping", Message: "Waiting for ModelGroups to stop"}
		if stopped {
			target = metav1.Condition{Status: metav1.ConditionTrue, Reason: "ScaledToZero", Message: "All ModelGroups have stopped"}
		}
		return ctrl.Result{}, reconciler.updateStatus(ctx, pool, metav1.ConditionUnknown, "NotRequired", "Stopping uses the existing Group execution contracts", groupState{
			Materialized: stopped, RolloutPending: !stopped, Target: target,
			Reason: target.Reason, Message: target.Message,
		})
	}
	// Preserve the active limit until this Pool reflects the owning service's current intent.
	if !reflect.DeepEqual(pool.Spec.InstanceAdmission, service.Spec.InstanceAdmission) {
		return ctrl.Result{}, nil
	}
	// The service owner must recover the executable selection before a legacy Pool can create a cohort.
	if pool.Spec.Template.Application == nil || pool.Spec.Template.Application.PreparationVersion == nil {
		return ctrl.Result{}, nil
	}
	template, err := resolver.ResolveModelPool(pool.Spec.Template, reconciler.RuntimeProfile)
	if err == nil && template.ECRuntime != nil {
		claim := new(corev1.PersistentVolumeClaim)
		if err := reader.Get(ctx, client.ObjectKey{Namespace: pool.Namespace, Name: template.ECRuntime.SharedStorageClaim}, claim); err != nil {
			state, stateErr := reconciler.currentActiveState(ctx, pool, servingconfig.ServingRevision(service, pool))
			state.Reason, state.Message = "StorageUnavailable", "Waiting for encoder transfer storage"
			statusErr := reconciler.updateStatus(ctx, pool, metav1.ConditionUnknown, "StorageUnavailable", err.Error(), state)
			return ctrl.Result{}, errors.Join(client.IgnoreNotFound(err), stateErr, statusErr)
		}
		template.ECRuntime.SharedStorageIdentity, err = runtimeconfig.ParseStorageIdentity(claim.Annotations[runtimeconfig.DirectoryOwnerAnnotation])
		if err == nil && template.ECRuntime.SharedStorageIdentity == nil {
			identity := runtimeconfig.DefaultStorageIdentity()
			template.ECRuntime.SharedStorageIdentity = &identity
		}
	}
	if err != nil {
		state, stateErr := reconciler.currentActiveState(ctx, pool, servingconfig.ServingRevision(service, pool))
		state.Reason, state.Message = "ResolutionFailed", "The target Pool execution config could not be resolved"
		statusErr := reconciler.updateStatus(ctx, pool, metav1.ConditionFalse, "ResolutionFailed", err.Error(), state)
		return ctrl.Result{}, errors.Join(stateErr, statusErr)
	}
	if err := reconciler.reconcileInstanceAdmission(ctx, pool, template); err != nil {
		return ctrl.Result{}, err
	}
	servingRevision := servingconfig.ServingRevision(service, pool)
	template.Revision, err = reconciler.targetRevision(ctx, pool, template, servingRevision)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.reconcileWeightSources(ctx, pool, template); err != nil {
		return ctrl.Result{}, err
	}
	// Establish the source reference before execution to cover publications racing with stop.
	targetSpec := template.Spec(pool, 0)
	if _, err := reconciler.reconcilePreparationSources(ctx, pool, service, &targetSpec); err != nil {
		return ctrl.Result{}, err
	}
	state, err := reconciler.reconcileGroups(ctx, pool, template, servingRevision, retiring)
	if err != nil {
		active, stateErr := reconciler.currentActiveState(ctx, pool, servingconfig.ServingRevision(service, pool))
		active.Reason, active.Message = "ApplyFailed", "ModelGroups were not fully materialized"
		statusErr := reconciler.updateStatus(ctx, pool, metav1.ConditionTrue, "Resolved", "Pool execution config was resolved", active)
		return ctrl.Result{}, errors.Join(err, stateErr, statusErr)
	}
	// Readiness may follow the first source read; adopt its publication before committing PreparedRevision.
	if state.PreparedRevision != "" {
		if _, err := reconciler.reconcilePreparationSources(ctx, pool, service, &targetSpec); err != nil {
			return ctrl.Result{}, err
		}
	}
	if err := reconciler.updateStatus(ctx, pool, metav1.ConditionTrue, "Resolved", "Pool execution config was resolved", state); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, nil
}

// retireExcessGroups shares the Group drain lifecycle between scale-down and Pool deletion.
// It reports pending retirement until every excess Group has released its workloads.
func (reconciler *PoolReconciler) retireExcessGroups(ctx context.Context, pool *inferencev1alpha1.ModelPool, desired int32) (bool, error) {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	groups, err := ownedModelGroups(ctx, reader, pool)
	if err != nil {
		return false, err
	}
	pending := false
	for index := range groups {
		group := &groups[index]
		if group.Spec.Ordinal < desired {
			continue
		}
		pending = true
		if err := reconciler.retireGroup(ctx, group); err != nil {
			return false, err
		}
	}
	return pending, nil
}

// retireGroup protects workload cleanup before requesting Group deletion.
func (reconciler *PoolReconciler) retireGroup(ctx context.Context, group *inferencev1alpha1.ModelGroup) error {
	if !group.DeletionTimestamp.IsZero() {
		return nil
	}
	if !controllerutil.ContainsFinalizer(group, modelGroupDrainFinalizer) {
		base := group.DeepCopy()
		controllerutil.AddFinalizer(group, modelGroupDrainFinalizer)
		if err := reconciler.Patch(ctx, group, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
			return fmt.Errorf("protect ModelGroup retirement: %w", err)
		}
	}
	return client.IgnoreNotFound(reconciler.Delete(ctx, group, client.Preconditions{UID: &group.UID}))
}

// validateModelServiceOwnership verifies that the referenced ModelService controls the ModelPool.
func (reconciler *PoolReconciler) validateModelServiceOwnership(ctx context.Context, pool *inferencev1alpha1.ModelPool) (*inferencev1alpha1.ModelService, error) {
	service := new(inferencev1alpha1.ModelService)
	key := client.ObjectKey{Namespace: pool.Namespace, Name: pool.Spec.ModelServiceRef.Name}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	if err := reader.Get(ctx, key, service); err != nil {
		return nil, fmt.Errorf("get owning ModelService: %w", err)
	}
	if pool.Spec.ModelServiceRef.UID != string(service.UID) || !metav1.IsControlledBy(pool, service) {
		return nil, fmt.Errorf("ModelPool %q is not owned by its referenced ModelService", pool.Name)
	}
	return service, nil
}

// targetRevision reuses a matching cohort or allocates an identity for changed execution settings.
func (reconciler *PoolReconciler) targetRevision(ctx context.Context, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate, servingRevision string) (string, error) {
	groups, err := ownedModelGroups(ctx, reconciler.Client, pool)
	if err != nil {
		return "", err
	}
	if revision := matchingRevision(groups, pool, template, servingRevision); revision != "" {
		return revision, nil
	}

	// A successful create can precede its informer event. Read current children before
	// allocating an identity so retries cannot materialize a second matching cohort.
	groups, err = ownedModelGroups(ctx, reconciler.APIReader, pool)
	if err != nil {
		return "", err
	}
	if revision := matchingRevision(groups, pool, template, servingRevision); revision != "" {
		return revision, nil
	}
	return string(uuid.NewUUID()), nil
}

// matchingRevision prefers the selected cohort, then reuses a matching target still starting.
func matchingRevision(groups []inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate, servingRevision string) string {
	for _, revision := range []string{pool.Status.PreparedRevision, servingRevision} {
		if revision == "" {
			continue
		}
		for index := range groups {
			group := &groups[index]
			if group.DeletionTimestamp.IsZero() && group.Spec.Revision == revision && groupMatchesTemplate(group, pool, template) {
				return revision
			}
		}
	}
	for index := range groups {
		group := &groups[index]
		if group.DeletionTimestamp.IsZero() && groupMatchesTemplate(group, pool, template) {
			return group.Spec.Revision
		}
	}
	return ""
}

func groupMatchesTemplate(group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate) bool {
	template.Revision = group.Spec.Revision
	return reflect.DeepEqual(group.Spec, template.Spec(pool, group.Spec.Ordinal))
}

// modelPoolStopped confirms the Pool controller has reconciled its current zero-capacity intent.
func modelPoolStopped(pool *inferencev1alpha1.ModelPool) bool {
	condition := meta.FindStatusCondition(pool.Status.Conditions, workload.ConditionTargetReady)
	return pool.Spec.DesiredGroups == 0 && pool.Status.ObservedGeneration == pool.Generation &&
		condition != nil && condition.ObservedGeneration == pool.Generation &&
		condition.Status == metav1.ConditionTrue && condition.Reason == "ScaledToZero"
}

// currentActiveState derives serving readiness while a target revision is being reconciled.
func (reconciler *PoolReconciler) currentActiveState(ctx context.Context, pool *inferencev1alpha1.ModelPool, servingRevision string) (groupState, error) {
	groups, err := ownedModelGroups(ctx, reconciler.Client, pool)
	if err != nil {
		return groupState{}, err
	}
	return groupState{
		Ready:          revisionServingReady(groups, servingRevision),
		RolloutPending: true,
	}, nil
}

// reconcileGroups converges target and serving ModelGroup cohorts for one ModelPool.
func (reconciler *PoolReconciler) reconcileGroups(ctx context.Context, pool *inferencev1alpha1.ModelPool, template resolver.ModelGroupTemplate, servingRevision string, retiring bool) (groupState, error) {
	groups, err := ownedModelGroups(ctx, reconciler.Client, pool)
	if err != nil {
		return groupState{}, err
	}
	current := make(map[int32]*inferencev1alpha1.ModelGroup, len(groups))
	for index := range groups {
		group := &groups[index]
		if group.Spec.Revision != template.Revision || group.Spec.Ordinal >= pool.Spec.DesiredGroups {
			continue
		}
		if previous := current[group.Spec.Ordinal]; previous != nil {
			return groupState{}, fmt.Errorf("ModelPool owns duplicate ModelGroups for revision %q ordinal %d", template.Revision, group.Spec.Ordinal)
		}
		current[group.Spec.Ordinal] = group
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
	targetReady := materialized && groupsReady(current, pool.Spec.DesiredGroups)
	targetInsufficientCapacity := materialized && groupsInsufficientCapacity(current, pool.Spec.DesiredGroups)
	// Only the current target can be prepared; ModelService separately retains the serving revision.
	preparedRevision := ""
	if targetReady {
		preparedRevision = template.Revision
	}
	servingRevisionDraining := targetInsufficientCapacity && servingRevision != "" && servingRevision != template.Revision
	ready := revisionServingReady(groups, servingRevision) && !servingRevisionDraining
	rolloutPending := preparedRevision != template.Revision || servingRevision != template.Revision || !targetReady

	// Keep the old cohort for zero-downtime replacement while the target can
	// schedule. If the target is explicitly unschedulable, retaining the old
	// cohort prevents a full cluster from ever making progress; drain it so the
	// target can acquire the same resources.
	if targetInsufficientCapacity && servingRevision != "" && servingRevision != template.Revision {
		for index := range groups {
			group := &groups[index]
			if group.Spec.Revision != servingRevision {
				continue
			}
			rolloutPending = true
			if err := reconciler.retireGroup(ctx, group); err != nil {
				return groupState{}, fmt.Errorf("delete unschedulable serving ModelGroup %q: %w", group.Name, err)
			}
		}
	}

	// The Pool keeps both its target cohort and the service-selected serving cohort.
	// Other revisions are no longer reachable and can enter their normal drain finalizer.
	for index := range groups {
		group := &groups[index]
		if group.Spec.Revision == template.Revision || group.Spec.Revision == servingRevision {
			continue
		}
		rolloutPending = true
		if err := reconciler.retireGroup(ctx, group); err != nil {
			return groupState{}, fmt.Errorf("delete superseded ModelGroup %q: %w", group.Name, err)
		}
	}

	state := groupState{
		Materialized:         materialized,
		Ready:                ready,
		CapacityReady:        targetReady,
		RolloutPending:       rolloutPending || retiring,
		InsufficientCapacity: !targetReady && targetInsufficientCapacity,
		PreparedRevision:     preparedRevision,
		Reason:               "Applied",
		Message:              "All requested ModelGroups were materialized",
	}
	state.Target = metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Starting", Message: "Waiting for requested ModelGroups to become ready"}
	if targetReady {
		state.Target = metav1.Condition{Status: metav1.ConditionTrue, Reason: "Ready", Message: "Requested ModelGroup capacity is ready"}
		if state.RolloutPending {
			state.Target = metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Converging", Message: "Waiting for serving selection and retired ModelGroups"}
		}
	} else {
		for ordinal := int32(0); ordinal < pool.Spec.DesiredGroups; ordinal++ {
			group := current[ordinal]
			if group == nil || !group.DeletionTimestamp.IsZero() {
				continue
			}
			failure := meta.FindStatusCondition(group.Status.Conditions, workload.ConditionTargetReady)
			if failure != nil && failure.ObservedGeneration == group.Generation && failure.Status == metav1.ConditionFalse {
				state.Target = metav1.Condition{Status: metav1.ConditionFalse, Reason: failure.Reason, Message: fmt.Sprintf("ModelGroup %s: %s", group.Name, failure.Message)}
				break
			}
		}
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
	condition := meta.FindStatusCondition(group.Status.Conditions, workload.ConditionReady)
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

// ownedModelGroups reads and verifies the Pool's children through the selected consistency boundary.
func ownedModelGroups(ctx context.Context, reader client.Reader, pool *inferencev1alpha1.ModelPool) ([]inferencev1alpha1.ModelGroup, error) {
	var list inferencev1alpha1.ModelGroupList
	if err := reader.List(ctx, &list, client.InNamespace(pool.Namespace)); err != nil {
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

// updateStatus publishes resolution, rollout, capacity, and serving readiness for the ModelPool.
func (reconciler *PoolReconciler) updateStatus(ctx context.Context, pool *inferencev1alpha1.ModelPool, resolvedStatus metav1.ConditionStatus, resolvedReason, resolvedMessage string, state groupState) error {
	if state.Reason == "" {
		state.Reason = "NotMaterialized"
		state.Message = "ModelGroups were not materialized"
	}
	if resolvedStatus == metav1.ConditionFalse {
		state.Target = metav1.Condition{Status: metav1.ConditionFalse, Reason: resolvedReason, Message: resolvedMessage}
	} else if state.Target.Status == "" {
		state.Target = metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Reconciling", Message: "Waiting for requested ModelGroups to be materialized"}
	}
	base := pool.DeepCopy()
	pool.Status.ObservedGeneration = pool.Generation
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: workload.ConditionTargetReady, Status: state.Target.Status, Reason: state.Target.Reason, Message: state.Target.Message, ObservedGeneration: pool.Generation})
	pool.Status.PreparedRevision = state.PreparedRevision
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionResolved, Status: resolvedStatus, Reason: resolvedReason, Message: resolvedMessage, ObservedGeneration: pool.Generation})
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: workload.ConditionGroupsMaterialized, Status: workload.ConditionStatus(state.Materialized), Reason: state.Reason, Message: state.Message, ObservedGeneration: pool.Generation})
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: conditionRolloutPending, Status: workload.ConditionStatus(state.RolloutPending), Reason: rolloutReason(state), Message: rolloutMessage(state), ObservedGeneration: pool.Generation})
	readyReason, readyMessage := poolReadyReasonMessage(state.Ready, state.CapacityReady)
	if pool.Spec.DesiredGroups == 0 {
		readyReason, readyMessage = state.Target.Reason, state.Target.Message
	}
	meta.SetStatusCondition(&pool.Status.Conditions, metav1.Condition{Type: workload.ConditionReady, Status: workload.ConditionStatus(state.Ready), Reason: readyReason, Message: readyMessage, ObservedGeneration: pool.Generation})
	if reflect.DeepEqual(base.Status, pool.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, pool, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelPool status: %w", err)
	}
	return nil
}

func poolReadyReasonMessage(ready, capacityReady bool) (string, string) {
	if ready && !capacityReady {
		return "ServingRevisionReady", "The service-selected revision remains ready while requested capacity is converging"
	}
	if ready {
		return "Ready", "All requested ModelGroups are ready"
	}
	return "GroupsNotReady", "One or more requested ModelGroups are not ready"
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
		return "The target Group revision is Unschedulable; the serving revision is being drained"
	}
	if state.RolloutPending {
		return "Requested Group capacity is converging or superseded Groups are being retired"
	}
	return "No previous Group revision is pending rollout"
}

// SetupWithManager registers the ModelPool controller and its owned Groups.
func (reconciler *PoolReconciler) SetupWithManager(manager ctrl.Manager) error {
	reconciler.APIReader = manager.GetAPIReader()
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.ModelPool{}, ctrlbuilder.WithPredicates(predicate.Funcs{
			UpdateFunc: func(update event.UpdateEvent) bool {
				// A reservation is durable publication state, not a new input.
				// Failed ConfigMap writes must retain controller-runtime's error backoff.
				before := update.ObjectOld.(*inferencev1alpha1.ModelPool).DeepCopy()
				after := update.ObjectNew.(*inferencev1alpha1.ModelPool)
				before.ResourceVersion, before.ManagedFields = after.ResourceVersion, after.ManagedFields
				before.Status.InstanceAdmissionVersion = after.Status.InstanceAdmissionVersion
				return !reflect.DeepEqual(before, after)
			},
		})).
		Owns(&inferencev1alpha1.ModelGroup{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.ConfigMap{}).
		Watches(&corev1.PersistentVolumeClaim{}, handler.EnqueueRequestsFromMapFunc(reconciler.poolsForStorageClaim)).
		Watches(&corev1.Pod{}, handler.EnqueueRequestsFromMapFunc(reconciler.poolsForInstancePod)).
		Watches(&inferencev1alpha1.ModelService{}, handler.EnqueueRequestsFromMapFunc(reconciler.poolsForService)).
		Complete(reconciler)
}

// poolsForStorageClaim wakes pools whose encoder transfer uses a changed claim.
func (reconciler *PoolReconciler) poolsForStorageClaim(ctx context.Context, object client.Object) []reconcile.Request {
	var pools inferencev1alpha1.ModelPoolList
	if err := reconciler.List(ctx, &pools, client.InNamespace(object.GetNamespace())); err != nil {
		ctrl.LoggerFrom(ctx).Error(err, "list pools for encoder storage claim")
		return nil
	}
	var requests []reconcile.Request
	for index := range pools.Items {
		pool := &pools.Items[index]
		template, err := resolver.ResolveModelPool(pool.Spec.Template, reconciler.RuntimeProfile)
		if err == nil && template.ECRuntime != nil && template.ECRuntime.SharedStorageClaim == object.GetName() {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(pool)})
		}
	}
	return requests
}

// poolsForService maps a ModelService update to its owned ModelPools.
func (reconciler *PoolReconciler) poolsForService(ctx context.Context, object client.Object) []reconcile.Request {
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
