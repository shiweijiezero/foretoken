// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Withdraws routes and drains accepted requests before retiring model workloads.

package model

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

const (
	modelGroupDrainFinalizer    = "inference.foretoken.io/modelgroup-drain"
	conditionRoutingWithdrawn   = "RoutingWithdrawn"
	conditionAdmissionClosed    = "AdmissionClosed"
	conditionDrained            = "Drained"
	modelGroupDrainPollInterval = time.Second
)

type modelServerTelemetry struct {
	Version                  uint8   `json:"version"`
	CollectedAtUnixMS        uint64  `json:"collected_at_unix_ms"`
	Accepting                bool    `json:"accepting"`
	RunningRequests          uint64  `json:"running_requests"`
	SchedulerRunningRequests *uint64 `json:"scheduler_running_requests"`
	SchedulerWaitingRequests *uint64 `json:"scheduler_waiting_requests"`
}

// reconcileDelete coordinates admission closure, route withdrawal, and bounded request drain before deletion.
func (reconciler *GroupReconciler) reconcileDelete(ctx context.Context, group *inferencev1alpha1.ModelGroup) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(group, modelGroupDrainFinalizer) {
		return ctrl.Result{}, nil
	}

	workloads, err := reconciler.groupWorkloads(ctx, group)
	if err != nil {
		return ctrl.Result{}, err
	}
	serving := false
	for _, executionWorkload := range workloads {
		if _, preparation := executionWorkload.(*batchv1.Job); !preparation {
			serving = true
		}
	}
	if serving {
		reader := reconciler.APIReader
		if reader == nil {
			reader = reconciler.Client
		}
		var pods corev1.PodList
		if err := reader.List(ctx, &pods, client.InNamespace(group.Namespace), client.MatchingLabels{workload.ModelGroupLabel: group.Name}); err != nil {
			return ctrl.Result{}, fmt.Errorf("read execution Pods before drain: %w", err)
		}
		// An unscheduled or finished Pod cannot hold accepted requests. Do not
		// wait on an admission endpoint that has never started or already exited.
		serving = false
		for _, pod := range pods.Items {
			if pod.Spec.NodeName != "" && pod.Status.Phase != corev1.PodSucceeded && pod.Status.Phase != corev1.PodFailed {
				serving = true
				break
			}
		}
	}
	drained := meta.FindStatusCondition(group.Status.Conditions, conditionDrained)
	if !serving || (drained != nil && (drained.Status == metav1.ConditionTrue || drained.Reason == "DrainTimedOut")) {
		return reconciler.retireGroupWorkloads(ctx, group, workloads)
	}

	now := time.Now()
	if group.Status.DrainStartedAt == nil {
		started := metav1.NewTime(now)
		if err := reconciler.updateDrainStatus(ctx, group, &started, false, false, false, "DrainStarted", "Closing model-server admission before route withdrawal"); err != nil {
			return ctrl.Result{}, err
		}
	}

	timeout, err := time.ParseDuration(string(group.Spec.Timeouts.Drain))
	if err != nil || timeout <= 0 {
		return ctrl.Result{}, fmt.Errorf("invalid ModelGroup drain timeout %q", group.Spec.Timeouts.Drain)
	}
	remaining := group.Status.DrainStartedAt.Add(timeout).Sub(now)
	if remaining <= 0 {
		return reconciler.finishDrainTimeout(ctx, group)
	}
	drainCtx, cancel := context.WithTimeout(ctx, remaining)
	defer cancel()

	telemetry, err := closeModelAdmission(drainCtx, workload.ModelGroupEndpoint(group, group.Spec.Runtime.Port))
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(drainCtx.Err(), context.DeadlineExceeded) {
		return reconciler.finishDrainTimeout(ctx, group)
	}
	if err != nil {
		routingWithdrawn := meta.IsStatusConditionTrue(group.Status.Conditions, conditionRoutingWithdrawn)
		if statusErr := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, routingWithdrawn, false, false, "AdmissionCloseFailed", err.Error()); statusErr != nil {
			return ctrl.Result{}, statusErr
		}
		return ctrl.Result{RequeueAfter: modelGroupDrainPollInterval}, nil
	}
	if telemetry.Accepting {
		routingWithdrawn := meta.IsStatusConditionTrue(group.Status.Conditions, conditionRoutingWithdrawn)
		if err := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, routingWithdrawn, false, false, "AdmissionCloseFailed", "model-server remained accepting after admission close"); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: modelGroupDrainPollInterval}, nil
	}
	if !meta.IsStatusConditionTrue(group.Status.Conditions, conditionAdmissionClosed) {
		if err := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, false, true, false, "AdmissionClosed", "model-server admission is closed"); err != nil {
			return ctrl.Result{}, err
		}
	}

	withdrawn, err := reconciler.routingWithdrawn(drainCtx, group)
	if errors.Is(err, context.DeadlineExceeded) || errors.Is(drainCtx.Err(), context.DeadlineExceeded) {
		return reconciler.finishDrainTimeout(ctx, group)
	}
	if err != nil {
		return ctrl.Result{}, err
	}
	if !withdrawn {
		if err := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, false, true, false, "WaitingForRouting", "Waiting for every frontend Pod to install the withdrawal snapshot"); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: modelGroupDrainPollInterval}, nil
	}
	if telemetry.RunningRequests > 0 {
		message := fmt.Sprintf("Waiting for %d running requests to finish", telemetry.RunningRequests)
		if err := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, true, true, false, "RequestsRunning", message); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: modelGroupDrainPollInterval}, nil
	}

	if err := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, true, true, true, "Drained", "Admission was closed, routing was withdrawn, and all accepted requests finished"); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{Requeue: true}, nil
}

// validateVersion accepts telemetry schemas whose admission and load fields are compatible.
func (telemetry modelServerTelemetry) validateVersion() error {
	if telemetry.Version != 2 && telemetry.Version != 3 {
		return fmt.Errorf("unsupported model-server telemetry version %d", telemetry.Version)
	}
	return nil
}

// closeModelAdmission closes model-server admission and returns its current drain telemetry.
func closeModelAdmission(ctx context.Context, endpoint string) (modelServerTelemetry, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint+"/v1/internal/admission/close", nil)
	if err != nil {
		return modelServerTelemetry{}, err
	}
	response, err := http.DefaultClient.Do(request)
	if err != nil {
		return modelServerTelemetry{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return modelServerTelemetry{}, fmt.Errorf("admission close returned HTTP %d", response.StatusCode)
	}
	var telemetry modelServerTelemetry
	if err := json.NewDecoder(response.Body).Decode(&telemetry); err != nil {
		return modelServerTelemetry{}, fmt.Errorf("decode admission close response: %w", err)
	}
	if err := telemetry.validateVersion(); err != nil {
		return modelServerTelemetry{}, err
	}
	return telemetry, nil
}

// groupWorkloads reads the Group's preparation and serving owners before retirement.
func (reconciler *GroupReconciler) groupWorkloads(ctx context.Context, group *inferencev1alpha1.ModelGroup) ([]client.Object, error) {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	objects := []client.Object{
		&appsv1.Deployment{ObjectMeta: metav1.ObjectMeta{Namespace: group.Namespace, Name: group.Name}},
		&batchv1.Job{ObjectMeta: metav1.ObjectMeta{Namespace: group.Namespace, Name: "prepare-" + string(group.UID)}},
		&batchv1.Job{ObjectMeta: metav1.ObjectMeta{Namespace: group.Namespace, Name: runtimePreparationName(group)}},
	}
	if reconciler.LeaderWorkerSets {
		objects = append(objects, &lwsv1.LeaderWorkerSet{ObjectMeta: metav1.ObjectMeta{Namespace: group.Namespace, Name: workload.ModelGroupLeaderWorkerSetName(group)}})
		if workload.ModelGroupLeaderWorkerSetName(group) != group.Name {
			objects = append(objects, &lwsv1.LeaderWorkerSet{ObjectMeta: metav1.ObjectMeta{Namespace: group.Namespace, Name: group.Name}})
		}
	}
	var workloads []client.Object
	for _, object := range objects {
		if err := reader.Get(ctx, client.ObjectKeyFromObject(object), object); apierrors.IsNotFound(err) {
			continue
		} else if err != nil {
			return nil, fmt.Errorf("get ModelGroup workload: %w", err)
		}
		if !metav1.IsControlledBy(object, group) {
			return nil, fmt.Errorf("workload %q is not controlled by ModelGroup", object.GetName())
		}
		workloads = append(workloads, object)
	}
	return workloads, nil
}

// retireGroupWorkloads removes execution after drain and retains the Group while
// Kubernetes terminates its Pods, so Pool and Service stop observations are complete.
func (reconciler *GroupReconciler) retireGroupWorkloads(ctx context.Context, group *inferencev1alpha1.ModelGroup, workloads []client.Object) (ctrl.Result, error) {
	for _, executionWorkload := range workloads {
		if !executionWorkload.GetDeletionTimestamp().IsZero() {
			continue
		}
		uid := executionWorkload.GetUID()
		if err := reconciler.Delete(ctx, executionWorkload, client.PropagationPolicy(metav1.DeletePropagationForeground), client.Preconditions{UID: &uid}); client.IgnoreNotFound(err) != nil {
			return ctrl.Result{}, fmt.Errorf("retire ModelGroup workload: %w", err)
		}
	}
	if len(workloads) > 0 {
		return ctrl.Result{RequeueAfter: modelGroupDrainPollInterval}, nil
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	// Include Pods left behind by an externally deleted workload controller.
	for _, label := range []string{workload.ModelGroupLabel, workload.ModelPreparationGroupLabel} {
		var pods corev1.PodList
		if err := reader.List(ctx, &pods, client.InNamespace(group.Namespace), client.MatchingLabels{label: group.Name}); err != nil {
			return ctrl.Result{}, err
		}
		for _, pod := range pods.Items {
			if pod.Status.Phase != corev1.PodSucceeded && pod.Status.Phase != corev1.PodFailed {
				return ctrl.Result{RequeueAfter: modelGroupDrainPollInterval}, nil
			}
		}
	}
	return ctrl.Result{}, reconciler.removeDrainFinalizer(ctx, group)
}

// finishDrainTimeout records an incomplete drain before workload termination.
func (reconciler *GroupReconciler) finishDrainTimeout(ctx context.Context, group *inferencev1alpha1.ModelGroup) (ctrl.Result, error) {
	routingWithdrawn := meta.IsStatusConditionTrue(group.Status.Conditions, conditionRoutingWithdrawn)
	admissionClosed := meta.IsStatusConditionTrue(group.Status.Conditions, conditionAdmissionClosed)
	if err := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, routingWithdrawn, admissionClosed, false, "DrainTimedOut", "The bounded ModelGroup drain deadline expired"); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{Requeue: true}, nil
}

// routingWithdrawn verifies that every frontend snapshot and replica has observed route withdrawal.
func (reconciler *GroupReconciler) routingWithdrawn(ctx context.Context, group *inferencev1alpha1.ModelGroup) (bool, error) {
	var frontends inferencev1alpha1.FrontendServiceList
	if err := reconciler.List(ctx, &frontends, client.InNamespace(group.Namespace)); err != nil {
		return false, fmt.Errorf("list FrontendServices for drain: %w", err)
	}
	routeTargetID := string(group.UID)
	for index := range frontends.Items {
		frontend := &frontends.Items[index]
		if !frontend.DeletionTimestamp.IsZero() {
			continue
		}
		if frontend.Spec.Suspend || frontend.Spec.Replicas != nil && *frontend.Spec.Replicas == 0 {
			reader := reconciler.APIReader
			if reader == nil {
				reader = reconciler.Client
			}
			latest := new(inferencev1alpha1.FrontendService)
			if err := reader.Get(ctx, client.ObjectKeyFromObject(frontend), latest); err != nil {
				return false, client.IgnoreNotFound(err)
			}
			// A confirmed stop has no live routing consumers. Retained snapshots
			// may still contain old routes, but cannot delay backend retirement.
			if latest.UID == frontend.UID && servingconfig.FrontendStopped(latest) {
				continue
			}
		}
		var snapshotVersion uint64
		withdrawnViews := make(map[string]bool)
		for index, protocol := range servingconfig.ProtocolVersions(frontend) {
			configMap := new(corev1.ConfigMap)
			key := client.ObjectKey{Namespace: frontend.Namespace, Name: servingconfig.ConfigMapName(frontend, protocol)}
			if err := reconciler.Get(ctx, key, configMap); apierrors.IsNotFound(err) {
				if index == 0 {
					return false, nil
				}
				continue
			} else if err != nil {
				return false, fmt.Errorf("get frontend serving snapshot for drain: %w", err)
			}
			if !metav1.IsControlledBy(configMap, frontend) {
				return false, fmt.Errorf("ConfigMap %q is not controlled by FrontendService", configMap.Name)
			}
			var snapshot servingconfig.Snapshot
			if err := json.Unmarshal([]byte(configMap.Data[servingconfig.SnapshotKey]), &snapshot); err != nil {
				return false, fmt.Errorf("decode frontend serving snapshot for drain: %w", err)
			}
			if servingconfig.ContainsRouteTarget(snapshot, routeTargetID) || snapshot.Version == 0 {
				return false, nil
			}
			if index == 0 {
				snapshotVersion = snapshot.Version
			} else if snapshot.Version != snapshotVersion {
				return false, nil
			}
			withdrawnViews[configMap.Name] = true
		}

		var pods corev1.PodList
		if err := reconciler.List(ctx, &pods, client.InNamespace(frontend.Namespace), client.MatchingLabels{workload.FrontendServiceLabel: frontend.Name}); err != nil {
			return false, fmt.Errorf("list frontend Pods for drain: %w", err)
		}
		for podIndex := range pods.Items {
			pod := &pods.Items[podIndex]
			if !pod.DeletionTimestamp.IsZero() || pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
				continue
			}
			if !withdrawnViews[servingconfig.PodConfigMapName(pod)] {
				return false, nil
			}
			// Observe every surviving replica, including those made unready by the
			// withdrawal of their last backend. Serving readiness is not a routing ACK.
			endpoint, err := workload.FrontendPodEndpoint(pod)
			if err != nil {
				return false, err
			}
			diagnostics, err := servingconfig.ReadDiagnostics(ctx, http.DefaultClient, endpoint)
			if err != nil {
				return false, fmt.Errorf("observe frontend Pod %q: %w", pod.Name, err)
			}
			if diagnostics.ActiveGeneration == nil {
				return false, fmt.Errorf("observe frontend Pod %q: frontend has no active serving generation", pod.Name)
			}
			if *diagnostics.ActiveGeneration < snapshotVersion {
				return false, nil
			}
		}
		// A legacy write may succeed before the current protocol's publication. Check the
		// durable floor after Pod observation, including versions not yet loaded by any Pod.
		reader := reconciler.APIReader
		if reader == nil {
			reader = reconciler.Client
		}
		latest := new(inferencev1alpha1.FrontendService)
		if err := reader.Get(ctx, client.ObjectKeyFromObject(frontend), latest); err != nil {
			return false, client.IgnoreNotFound(err)
		}
		if latest.UID != frontend.UID || latest.Status.ServingSnapshotVersion != snapshotVersion || latest.Status.ServingConfigVersion != frontend.Status.ServingConfigVersion {
			return false, nil
		}
	}
	return true, nil
}

// updateDrainStatus publishes the current ModelGroup deletion-drain milestone.
func (reconciler *GroupReconciler) updateDrainStatus(ctx context.Context, group *inferencev1alpha1.ModelGroup, started *metav1.Time, routingWithdrawn, admissionClosed, drained bool, reason, message string) error {
	base := group.DeepCopy()
	group.Status.Phase = inferencev1alpha1.ModelGroupPhaseDraining
	group.Status.DrainStartedAt = started.DeepCopy()
	group.Status.ReadyMembers = 0
	group.Status.TotalMembers = group.Spec.MemberCount
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: workload.ConditionReady, Status: metav1.ConditionFalse, Reason: "Draining", Message: "The ModelGroup is draining before deletion", ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionRoutingWithdrawn, Status: workload.ConditionStatus(routingWithdrawn), Reason: reason, Message: message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionAdmissionClosed, Status: workload.ConditionStatus(admissionClosed), Reason: reason, Message: message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionDrained, Status: workload.ConditionStatus(drained), Reason: reason, Message: message, ObservedGeneration: group.Generation})
	if reflect.DeepEqual(base.Status, group.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, group, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelGroup drain status: %w", err)
	}
	return nil
}

// removeDrainFinalizer releases ModelGroup deletion after the drain lifecycle completes.
func (reconciler *GroupReconciler) removeDrainFinalizer(ctx context.Context, group *inferencev1alpha1.ModelGroup) error {
	base := group.DeepCopy()
	controllerutil.RemoveFinalizer(group, modelGroupDrainFinalizer)
	if err := reconciler.Patch(ctx, group, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("remove ModelGroup drain finalizer: %w", err)
	}
	return nil
}
