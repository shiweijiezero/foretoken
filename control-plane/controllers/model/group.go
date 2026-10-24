// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles one execution group and publishes preparation and workload status.

package model

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

const (
	conditionWorkloadMaterialized = "WorkloadMaterialized"
	conditionModelPreparation     = "ModelPreparation"
	conditionWorkloadAvailable    = "WorkloadAvailable"
	conditionSchedulingCapacity   = "SchedulingCapacity"
	multusNetworksAnnotation      = "k8s.v1.cni.cncf.io/networks"
	modelGroupFieldOwner          = "foretoken-modelgroup-controller"
	metricsScraperNamespaceLabel  = "inference.foretoken.io/metrics-scraper"
	metricsScraperNamespaceValue  = "true"
)

// GroupReconciler owns the Kubernetes workload for one execution Group.
type GroupReconciler struct {
	client.Client
	APIReader             client.Reader
	ControlPlaneNamespace string
	ImagePullSecrets      []corev1.LocalObjectReference
	ModelDistribution     runtimeconfig.ModelDistributionProfile
	LeaderWorkerSets      bool
	ApplicationFiles      runtimeconfig.ApplicationFiles
}

type modelGroupConditionState struct {
	status  metav1.ConditionStatus
	reason  string
	message string
}

type schedulingCapacityState = modelGroupConditionState

type modelGroupStatusState struct {
	phase               inferencev1alpha1.ModelGroupPhase
	observedWorkloadUID string
	preparation         modelGroupConditionState
	materialized        modelGroupConditionState
	available           bool
	unavailableSince    metav1.Time
	target              metav1.Condition
	scheduling          schedulingCapacityState
}

// Reconcile materializes the workload and derives readiness from the model server.
func (reconciler *GroupReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	group := new(inferencev1alpha1.ModelGroup)
	if err := reconciler.Get(ctx, request.NamespacedName, group); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !group.DeletionTimestamp.IsZero() {
		return reconciler.reconcileDelete(ctx, group)
	}
	pool, err := reconciler.owningModelPool(ctx, group)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := runtimeconfig.ValidateSourceRevision(group.Spec.Runtime.SourceRevision); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, group, modelGroupFailureState(err))
	}
	if err := validateGroupProfile(group); err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, group, modelGroupFailureState(err))
	}
	if group.Spec.NodeCount > 1 && !reconciler.LeaderWorkerSets {
		return ctrl.Result{}, reconciler.updateStatus(ctx, group, modelGroupFailureState(fmt.Errorf("multi-node Groups require LeaderWorkerSet; run foretoken install to prepare the controller")))
	}
	// Report configuration errors before allocating preparation or serving resources.
	deployment, err := desiredDeployment(group, reconciler.ImagePullSecrets, reconciler.ApplicationFiles)
	if err != nil {
		return ctrl.Result{}, reconciler.updateStatus(ctx, group, modelGroupFailureState(err))
	}
	if err := servingconfig.EnsureKVIndexerSecret(ctx, reconciler.Client, group.Namespace); err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.reconcileNetworkPolicy(ctx, group); err != nil {
		return ctrl.Result{}, err
	}
	// Hold the Group until preparation and serving workloads have both retired.
	if !controllerutil.ContainsFinalizer(group, modelGroupDrainFinalizer) {
		base := group.DeepCopy()
		controllerutil.AddFinalizer(group, modelGroupDrainFinalizer)
		if err := reconciler.Patch(ctx, group, client.MergeFrom(base)); err != nil {
			return ctrl.Result{}, fmt.Errorf("add ModelGroup drain finalizer: %w", err)
		}
		return ctrl.Result{Requeue: true}, nil
	}
	prepared, err := reconciler.reconcilePreparation(ctx, group, pool)
	if err == errNoPreparationPlacement {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, reconciler.updateStatus(ctx, group, modelGroupPlacementState())
	}
	if err != nil {
		var failure *modelPreparationFailure
		if errors.As(err, &failure) {
			return ctrl.Result{}, err
		}
		return ctrl.Result{}, errors.Join(err, reconciler.updateStatus(ctx, group, modelGroupPreparationFailureState(err)))
	}
	if !prepared {
		return ctrl.Result{}, reconciler.updateStatus(ctx, group, modelGroupPreparationState())
	}

	executionWorkload, err := reconciler.reconcileWorkload(ctx, group, deployment)
	if err != nil {
		// Retain previously admitted execution while repair waits; reject an unallocated target.
		if errors.Is(err, errKueueAdmissionInactive) && executionWorkload.uid == "" {
			state := modelGroupMaterializedState(false, schedulingNotEvaluated())
			state.phase = inferencev1alpha1.ModelGroupPhaseFailed
			state.materialized = modelGroupConditionState{status: metav1.ConditionFalse, reason: "QueueAdmissionUnavailable", message: err.Error()}
			state.target = metav1.Condition{Status: metav1.ConditionFalse, Reason: "QueueAdmissionUnavailable", Message: err.Error()}
			err = errors.Join(err, reconciler.updateStatus(ctx, group, state))
		}
		return ctrl.Result{}, err
	}
	if err := reconciler.reconcileService(ctx, group, pool); err != nil {
		return ctrl.Result{}, err
	}
	pods, err := reconciler.currentWorkloadPods(ctx, group, executionWorkload.uid)
	if err != nil {
		return ctrl.Result{}, err
	}
	// Pod events can arrive before the multi-node controller withdraws availability.
	if group.Spec.NodeCount > 1 && executionWorkload.available {
		executionWorkload.available = len(pods) == int(group.Spec.MemberCount)
		for index := range pods {
			if !pods[index].DeletionTimestamp.IsZero() || !workload.PodReady(&pods[index]) {
				executionWorkload.available = false
				break
			}
		}
	}
	state := modelGroupMaterializedState(executionWorkload.available, schedulingCapacity(group, pods))
	state.observedWorkloadUID = string(executionWorkload.uid)
	var remaining time.Duration
	if executionWorkload.uid == "" {
		state.materialized = modelGroupConditionState{status: metav1.ConditionFalse, reason: "ReplacingWorkload", message: "The serving workload waits for its previous workload to be retired"}
	} else if !executionWorkload.available {
		startup, err := time.ParseDuration(string(group.Spec.Timeouts.Startup))
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("parse ModelGroup startup timeout: %w", err)
		}
		now := time.Now()
		started := firstContainerStart(pods, deployment.Spec.Template.Spec.Containers[0].Name)
		// Once execution starts, retain one unavailable interval across restarts
		// and member replacement. Cold scheduling and image pulls precede that clock.
		if previous := meta.FindStatusCondition(group.Status.Conditions, conditionWorkloadAvailable); previous != nil {
			if group.Status.ObservedWorkloadUID == state.observedWorkloadUID {
				switch previous.Status {
				case metav1.ConditionFalse:
					started = previous.LastTransitionTime.Time
				case metav1.ConditionTrue:
					started = now
				}
			} else if group.Status.ObservedWorkloadUID == "" && previous.Status != metav1.ConditionUnknown && !started.IsZero() {
				// Older status has no workload identity; adopt one execution window when observing it.
				started = now
			}
		}
		state.target = metav1.Condition{Status: metav1.ConditionUnknown, Reason: "Starting", Message: "Waiting for the serving workload to become ready"}
		if !started.IsZero() {
			state.unavailableSince = metav1.NewTime(started)
			remaining = started.Add(startup).Sub(now)
			if remaining <= 0 {
				state.phase = inferencev1alpha1.ModelGroupPhaseFailed
				state.target = metav1.Condition{Status: metav1.ConditionFalse, Reason: "StartupDeadlineExceeded", Message: fmt.Sprintf("The serving workload did not become ready within %s; Kubernetes recovery continues", group.Spec.Timeouts.Startup)}
				remaining = 0
			}
		}
	}
	if err := reconciler.updateStatus(ctx, group, state); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{RequeueAfter: remaining}, nil
}

// owningModelPool returns the verified owner for workload reconciliation and scrape identity.
func (reconciler *GroupReconciler) owningModelPool(ctx context.Context, group *inferencev1alpha1.ModelGroup) (*inferencev1alpha1.ModelPool, error) {
	pool := new(inferencev1alpha1.ModelPool)
	key := client.ObjectKey{Namespace: group.Namespace, Name: group.Spec.ModelPoolRef.Name}
	if err := reconciler.Get(ctx, key, pool); err != nil {
		return nil, fmt.Errorf("get owning ModelPool: %w", err)
	}
	if group.Spec.ModelPoolRef.UID != string(pool.UID) || !metav1.IsControlledBy(group, pool) {
		return nil, fmt.Errorf("ModelGroup %q is not owned by its referenced ModelPool", group.Name)
	}
	if pd := group.Spec.PDRuntime; pd != nil && (pd.ServiceUID == "" || pd.ServiceUID != pool.Spec.ModelServiceRef.UID) {
		return nil, fmt.Errorf("ModelGroup P/D network scope does not match its owning ModelService")
	}
	if ec := group.Spec.ECRuntime; ec != nil && (ec.ServiceUID == "" || ec.ServiceUID != pool.Spec.ModelServiceRef.UID) {
		return nil, fmt.Errorf("ModelGroup encoder cache scope does not match its owning ModelService")
	}
	return pool, nil
}

func schedulingNotEvaluated() schedulingCapacityState {
	return schedulingCapacityState{status: metav1.ConditionUnknown, reason: "NotEvaluated", message: "Scheduling capacity was not evaluated"}
}

func modelGroupFailureState(err error) modelGroupStatusState {
	return modelGroupStatusState{
		phase:        inferencev1alpha1.ModelGroupPhaseFailed,
		preparation:  modelGroupConditionState{status: metav1.ConditionUnknown, reason: "NotEvaluated", message: "Model preparation was not evaluated"},
		materialized: modelGroupConditionState{status: metav1.ConditionFalse, reason: "UnsupportedProfile", message: err.Error()},
		target:       metav1.Condition{Status: metav1.ConditionFalse, Reason: "UnsupportedProfile", Message: err.Error()},
		scheduling:   schedulingNotEvaluated(),
	}
}

func modelGroupPreparationState() modelGroupStatusState {
	return modelGroupStatusState{
		phase:        inferencev1alpha1.ModelGroupPhaseProvisioning,
		preparation:  modelGroupConditionState{status: metav1.ConditionFalse, reason: "PreparingModel", message: "Remote model sources and artifacts are being prepared"},
		materialized: modelGroupConditionState{status: metav1.ConditionFalse, reason: "PreparingModel", message: "The serving workload waits for model preparation"},
		scheduling:   schedulingNotEvaluated(),
	}
}

func modelGroupPlacementState() modelGroupStatusState {
	return modelGroupStatusState{
		phase:        inferencev1alpha1.ModelGroupPhaseProvisioning,
		preparation:  modelGroupConditionState{status: metav1.ConditionFalse, reason: "WaitingForPlacement", message: "No schedulable accelerator node matches the model preparation workload"},
		materialized: modelGroupConditionState{status: metav1.ConditionFalse, reason: "WaitingForPlacement", message: "The serving workload waits for model preparation placement"},
		scheduling:   schedulingNotEvaluated(),
	}
}

func modelGroupPreparationFailureState(err error) modelGroupStatusState {
	state := modelGroupStatusState{
		phase:        inferencev1alpha1.ModelGroupPhaseFailed,
		preparation:  modelGroupConditionState{status: metav1.ConditionFalse, reason: "ModelPreparationFailed", message: err.Error()},
		materialized: modelGroupConditionState{status: metav1.ConditionFalse, reason: "ModelPreparationFailed", message: "The serving workload was not materialized"},
		scheduling:   schedulingNotEvaluated(),
	}
	var failure *modelPreparationFailure
	if errors.As(err, &failure) {
		state.target = metav1.Condition{Status: metav1.ConditionFalse, Reason: failure.reason, Message: err.Error()}
	}
	return state
}

func modelGroupMaterializedState(available bool, scheduling schedulingCapacityState) modelGroupStatusState {
	phase := inferencev1alpha1.ModelGroupPhaseProvisioning
	if available {
		phase = inferencev1alpha1.ModelGroupPhaseReady
	}
	state := modelGroupStatusState{
		phase:        phase,
		preparation:  modelGroupConditionState{status: metav1.ConditionTrue, reason: preparationSourceReadyReason, message: "Model preparation completed using resolved source versions"},
		materialized: modelGroupConditionState{status: metav1.ConditionTrue, reason: "Applied", message: "Group workload was materialized"},
		available:    available,
		scheduling:   scheduling,
	}
	if available {
		state.target = metav1.Condition{Status: metav1.ConditionTrue, Reason: "Ready", Message: "The serving workload is ready"}
	}
	return state
}

// schedulingCapacity reports insufficient capacity only from the scheduler's
// explicit Unschedulable signal; image pulls and slow startup remain unknown.
func schedulingCapacity(group *inferencev1alpha1.ModelGroup, pods []corev1.Pod) schedulingCapacityState {
	scheduled := int32(0)
	for index := range pods {
		for _, condition := range pods[index].Status.Conditions {
			if condition.Type != corev1.PodScheduled {
				continue
			}
			if condition.Status == corev1.ConditionFalse && condition.Reason == corev1.PodReasonUnschedulable {
				return schedulingCapacityState{status: metav1.ConditionFalse, reason: "InsufficientCapacity", message: "The Kubernetes scheduler reported the Group Pod as Unschedulable"}
			}
			if condition.Status == corev1.ConditionTrue {
				scheduled++
			}
		}
	}
	if scheduled == group.Spec.MemberCount {
		return schedulingCapacityState{status: metav1.ConditionTrue, reason: "Scheduled", message: "The Group Pod was scheduled"}
	}
	return schedulingCapacityState{status: metav1.ConditionUnknown, reason: "WaitingForScheduling", message: "The Group Pod has not reported a scheduling result"}
}

// firstContainerStart observes execution rather than Pod creation, including failures between reconciliations.
func firstContainerStart(pods []corev1.Pod, name string) time.Time {
	var first time.Time
	for index := range pods {
		for _, status := range pods[index].Status.ContainerStatuses {
			if status.Name != name {
				continue
			}
			for _, state := range []corev1.ContainerState{status.State, status.LastTerminationState} {
				var started time.Time
				if state.Running != nil {
					started = state.Running.StartedAt.Time
				} else if state.Terminated != nil {
					started = state.Terminated.StartedAt.Time
				}
				if !started.IsZero() && (first.IsZero() || started.Before(first)) {
					first = started
				}
			}
		}
	}
	return first
}

func modelGroupDeploymentAvailable(deployment *appsv1.Deployment) bool {
	if deployment.Status.ObservedGeneration < deployment.Generation || deployment.Status.AvailableReplicas != 1 {
		return false
	}
	for _, condition := range deployment.Status.Conditions {
		if condition.Type == appsv1.DeploymentAvailable && condition.Status == corev1.ConditionTrue {
			return true
		}
	}
	return false
}

// updateStatus projects workload availability and scheduler capacity into ModelGroup status.
func (reconciler *GroupReconciler) updateStatus(ctx context.Context, group *inferencev1alpha1.ModelGroup, state modelGroupStatusState) error {
	base := group.DeepCopy()
	if group.Status.ObservedWorkloadUID != state.observedWorkloadUID {
		meta.RemoveStatusCondition(&group.Status.Conditions, conditionWorkloadAvailable)
		group.Status.ObservedWorkloadUID = state.observedWorkloadUID
	}
	// Keep a terminal preparation result visible through retries. Only a successful
	// preparation or a replacement Group can supersede that failed attempt.
	if state.preparation.status == metav1.ConditionFalse && state.target.Status == "" {
		previous := meta.FindStatusCondition(group.Status.Conditions, workload.ConditionTargetReady)
		if previous != nil && previous.ObservedGeneration == group.Generation && previous.Status == metav1.ConditionFalse &&
			(previous.Reason == "ModelPreparationFailed" || previous.Reason == "ModelPreparationDeadlineExceeded") {
			state.phase = inferencev1alpha1.ModelGroupPhaseFailed
			state.target = metav1.Condition{Status: previous.Status, Reason: previous.Reason, Message: previous.Message}
		}
	}
	group.Status.Phase = state.phase
	group.Status.TotalMembers = group.Spec.MemberCount
	if state.available {
		group.Status.ReadyMembers = group.Spec.MemberCount
	} else {
		group.Status.ReadyMembers = 0
	}
	if state.preparation.status == metav1.ConditionTrue && (group.Spec.Runtime.PreparationVersion == 0 || group.Spec.Artifacts.Source == inferencev1alpha1.ModelSourceLocal) {
		state.preparation = modelGroupConditionState{status: metav1.ConditionTrue, reason: "NotRequired", message: "Model acquisition is handled by the serving runtime"}
	}
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionModelPreparation, Status: state.preparation.status, Reason: state.preparation.reason, Message: state.preparation.message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionWorkloadMaterialized, Status: state.materialized.status, Reason: state.materialized.reason, Message: state.materialized.message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionSchedulingCapacity, Status: state.scheduling.status, Reason: state.scheduling.reason, Message: state.scheduling.message, ObservedGeneration: group.Generation})
	availability := metav1.Condition{Type: conditionWorkloadAvailable, Status: workload.ConditionStatus(state.available), Reason: availabilityReason(state.available), Message: availabilityMessage(state.available), ObservedGeneration: group.Generation, LastTransitionTime: metav1.NewTime(time.Now())}
	if state.materialized.status != metav1.ConditionTrue {
		availability.Status = metav1.ConditionUnknown
		availability.Reason = "NotMaterialized"
		availability.Message = state.materialized.message
	} else if !state.available {
		if state.unavailableSince.IsZero() {
			availability.Status = metav1.ConditionUnknown
			availability.Reason = "WaitingForContainer"
			availability.Message = "The serving container has not started"
		} else {
			availability.LastTransitionTime = state.unavailableSince
		}
	}
	meta.SetStatusCondition(&group.Status.Conditions, availability)
	if state.target.Status == "" {
		state.target = metav1.Condition{Status: metav1.ConditionUnknown, Reason: state.materialized.reason, Message: state.materialized.message}
	}
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: workload.ConditionTargetReady, Status: state.target.Status, Reason: state.target.Reason, Message: state.target.Message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: workload.ConditionReady, Status: workload.ConditionStatus(state.available), Reason: availabilityReason(state.available), Message: readinessMessage(state.available), ObservedGeneration: group.Generation})
	if reflect.DeepEqual(base.Status, group.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, group, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelGroup status: %w", err)
	}
	return nil
}

func availabilityReason(available bool) string {
	if available {
		return "Available"
	}
	return "Unavailable"
}

func availabilityMessage(available bool) string {
	if available {
		return "The Group workload is available"
	}
	return "The Group workload is not yet available"
}

func readinessMessage(ready bool) string {
	if ready {
		return "The group-local model server and vLLM EngineCore are ready"
	}
	return "The group-local model server or vLLM EngineCore is not yet ready"
}

// SetupWithManager registers the ModelGroup controller and its owned resources.
func (reconciler *GroupReconciler) SetupWithManager(manager ctrl.Manager) error {
	reconciler.APIReader = manager.GetAPIReader()
	builder := ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.ModelGroup{}).
		Owns(&batchv1.Job{}).
		Owns(&appsv1.Deployment{}).
		Owns(&corev1.Service{}).
		Owns(&networkingv1.NetworkPolicy{}).
		Watches(&corev1.Pod{}, handler.EnqueueRequestsFromMapFunc(reconciler.modelGroupsForPod))
	_, err := manager.GetRESTMapper().RESTMapping(lwsv1.GroupVersion.WithKind("LeaderWorkerSet").GroupKind(), lwsv1.GroupVersion.Version)
	if err == nil {
		reconciler.LeaderWorkerSets = true
		builder = builder.Owns(&lwsv1.LeaderWorkerSet{})
	} else if !meta.IsNoMatchError(err) {
		return fmt.Errorf("discover LeaderWorkerSet: %w", err)
	}
	return builder.Complete(reconciler)
}

// modelGroupsForPod requeues the execution Group when a member Pod changes.
// Workload controllers, rather than ModelGroups, directly own the Pods.
func (reconciler *GroupReconciler) modelGroupsForPod(_ context.Context, object client.Object) []reconcile.Request {
	groupName := object.GetLabels()[workload.ModelGroupLabel]
	if groupName == "" {
		groupName = object.GetLabels()[workload.ModelPreparationGroupLabel]
	}
	if groupName == "" {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: object.GetNamespace(), Name: groupName}}}
}
