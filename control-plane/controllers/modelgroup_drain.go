// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Coordinates route withdrawal and bounded request drain before ModelGroup deletion.

package controllers

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"reflect"
	"strconv"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const (
	modelGroupDrainFinalizer    = "inference.foretoken.io/modelgroup-drain"
	conditionRoutingWithdrawn   = "RoutingWithdrawn"
	conditionAdmissionClosed    = "AdmissionClosed"
	conditionDrained            = "Drained"
	modelGroupDrainPollInterval = time.Second
	frontendHTTPPortName        = "http"
	modelServerTelemetryVersion = 2
)

type drainTelemetry struct {
	Version               uint8  `json:"version"`
	Accepting             bool   `json:"accepting"`
	RunningRequests       uint64 `json:"running_requests"`
	MaxConcurrentRequests uint64 `json:"max_concurrent_requests"`
}

type frontendDiagnostics struct {
	ActiveGeneration *uint64 `json:"active_generation"`
}

// ModelGroupDrainClient observes frontend generations and controls group-local admission.
type ModelGroupDrainClient interface {
	FrontendGeneration(context.Context, string) (uint64, error)
	CloseAdmission(context.Context, string) (drainTelemetry, error)
}

type httpModelGroupDrainClient struct {
	client *http.Client
}

func newHTTPModelGroupDrainClient() ModelGroupDrainClient {
	return &httpModelGroupDrainClient{client: &http.Client{}}
}

func (client *httpModelGroupDrainClient) FrontendGeneration(ctx context.Context, endpoint string) (uint64, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodGet, endpoint+"/statusz", nil)
	if err != nil {
		return 0, err
	}
	response, err := client.client.Do(request)
	if err != nil {
		return 0, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("frontend status returned HTTP %d", response.StatusCode)
	}
	var diagnostics frontendDiagnostics
	if err := json.NewDecoder(response.Body).Decode(&diagnostics); err != nil {
		return 0, fmt.Errorf("decode frontend status: %w", err)
	}
	if diagnostics.ActiveGeneration == nil {
		return 0, fmt.Errorf("frontend has no active serving generation")
	}
	return *diagnostics.ActiveGeneration, nil
}

func (client *httpModelGroupDrainClient) CloseAdmission(ctx context.Context, endpoint string) (drainTelemetry, error) {
	request, err := http.NewRequestWithContext(ctx, http.MethodPost, endpoint+"/v1/internal/admission/close", nil)
	if err != nil {
		return drainTelemetry{}, err
	}
	response, err := client.client.Do(request)
	if err != nil {
		return drainTelemetry{}, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK {
		return drainTelemetry{}, fmt.Errorf("admission close returned HTTP %d", response.StatusCode)
	}
	var telemetry drainTelemetry
	if err := json.NewDecoder(response.Body).Decode(&telemetry); err != nil {
		return drainTelemetry{}, fmt.Errorf("decode admission close response: %w", err)
	}
	if telemetry.Version != modelServerTelemetryVersion {
		return drainTelemetry{}, fmt.Errorf("unsupported model-server telemetry version %d", telemetry.Version)
	}
	return telemetry, nil
}

func (reconciler *ModelGroupReconciler) reconcileDelete(ctx context.Context, group *inferencev1alpha1.ModelGroup) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(group, modelGroupDrainFinalizer) {
		return ctrl.Result{}, nil
	}

	now := reconciler.now()
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

	telemetry, err := reconciler.drainClient().CloseAdmission(drainCtx, modelGroupEndpoint(group, group.Spec.Runtime.Port))
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
		if err := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, false, true, false, "WaitingForRouting", "Waiting for every ready frontend Pod to install the withdrawal snapshot"); err != nil {
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
	return ctrl.Result{}, reconciler.removeDrainFinalizer(ctx, group)
}

func (reconciler *ModelGroupReconciler) finishDrainTimeout(ctx context.Context, group *inferencev1alpha1.ModelGroup) (ctrl.Result, error) {
	routingWithdrawn := meta.IsStatusConditionTrue(group.Status.Conditions, conditionRoutingWithdrawn)
	admissionClosed := meta.IsStatusConditionTrue(group.Status.Conditions, conditionAdmissionClosed)
	if err := reconciler.updateDrainStatus(ctx, group, group.Status.DrainStartedAt, routingWithdrawn, admissionClosed, false, "DrainTimedOut", "The bounded ModelGroup drain deadline expired"); err != nil {
		return ctrl.Result{}, err
	}
	return ctrl.Result{}, reconciler.removeDrainFinalizer(ctx, group)
}

func (reconciler *ModelGroupReconciler) routingWithdrawn(ctx context.Context, group *inferencev1alpha1.ModelGroup) (bool, error) {
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
		configMap := new(corev1.ConfigMap)
		key := client.ObjectKey{Namespace: frontend.Namespace, Name: frontendServingConfigMapName(frontend)}
		if err := reconciler.Get(ctx, key, configMap); err != nil {
			if apierrors.IsNotFound(err) {
				return false, nil
			}
			return false, fmt.Errorf("get frontend serving snapshot for drain: %w", err)
		}
		var snapshot servingSnapshot
		if err := json.Unmarshal([]byte(configMap.Data[servingSnapshotKey]), &snapshot); err != nil {
			return false, fmt.Errorf("decode frontend serving snapshot for drain: %w", err)
		}
		if servingSnapshotContainsRouteTarget(snapshot, routeTargetID) {
			return false, nil
		}

		var pods corev1.PodList
		if err := reconciler.List(ctx, &pods, client.InNamespace(frontend.Namespace), client.MatchingLabels{frontendServiceLabel: frontend.Name}); err != nil {
			return false, fmt.Errorf("list frontend Pods for drain: %w", err)
		}
		for podIndex := range pods.Items {
			pod := &pods.Items[podIndex]
			if !pod.DeletionTimestamp.IsZero() {
				continue
			}
			// Every surviving replica must observe the withdrawal before deletion. An unready
			// replica may later rejoin the Service, so it cannot be excluded from the barrier.
			if !podReady(pod) {
				return false, nil
			}
			endpoint, err := frontendPodEndpoint(pod)
			if err != nil {
				return false, err
			}
			generation, err := reconciler.drainClient().FrontendGeneration(ctx, endpoint)
			if err != nil {
				return false, fmt.Errorf("observe frontend Pod %q: %w", pod.Name, err)
			}
			if generation < snapshot.Version {
				return false, nil
			}
		}
	}
	return true, nil
}

func servingSnapshotContainsRouteTarget(snapshot servingSnapshot, routeTargetID string) bool {
	for _, group := range snapshot.Groups {
		if group.RouteTargetID == routeTargetID {
			return true
		}
	}
	for _, component := range snapshot.PDComponents {
		if component.RouteTargetID == routeTargetID {
			return true
		}
	}
	for _, component := range snapshot.EPDComponents {
		if component.RouteTargetID == routeTargetID {
			return true
		}
	}
	return false
}

func podReady(pod *corev1.Pod) bool {
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return false
}

func frontendPodEndpoint(pod *corev1.Pod) (string, error) {
	if pod.Status.PodIP == "" {
		return "", fmt.Errorf("ready frontend Pod %q has no Pod IP", pod.Name)
	}
	for _, container := range pod.Spec.Containers {
		for _, port := range container.Ports {
			if port.Name == frontendHTTPPortName {
				address := net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(port.ContainerPort)))
				return "http://" + address, nil
			}
		}
	}
	return "", fmt.Errorf("ready frontend Pod %q has no %q port", pod.Name, frontendHTTPPortName)
}

func (reconciler *ModelGroupReconciler) drainClient() ModelGroupDrainClient {
	if reconciler.DrainClient != nil {
		return reconciler.DrainClient
	}
	return newHTTPModelGroupDrainClient()
}

func (reconciler *ModelGroupReconciler) now() time.Time {
	if reconciler.Now != nil {
		return reconciler.Now()
	}
	return time.Now()
}

func (reconciler *ModelGroupReconciler) updateDrainStatus(ctx context.Context, group *inferencev1alpha1.ModelGroup, started *metav1.Time, routingWithdrawn, admissionClosed, drained bool, reason, message string) error {
	base := group.DeepCopy()
	group.Status.Phase = inferencev1alpha1.ModelGroupPhaseDraining
	group.Status.DrainStartedAt = started.DeepCopy()
	group.Status.ReadyMembers = 0
	group.Status.TotalMembers = group.Spec.MemberCount
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionReady, Status: metav1.ConditionFalse, Reason: "Draining", Message: "The ModelGroup is draining before deletion", ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionRoutingWithdrawn, Status: conditionStatus(routingWithdrawn), Reason: reason, Message: message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionAdmissionClosed, Status: conditionStatus(admissionClosed), Reason: reason, Message: message, ObservedGeneration: group.Generation})
	meta.SetStatusCondition(&group.Status.Conditions, metav1.Condition{Type: conditionDrained, Status: conditionStatus(drained), Reason: reason, Message: message, ObservedGeneration: group.Generation})
	if reflect.DeepEqual(base.Status, group.Status) {
		return nil
	}
	if err := reconciler.Status().Patch(ctx, group, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("update ModelGroup drain status: %w", err)
	}
	return nil
}

func (reconciler *ModelGroupReconciler) removeDrainFinalizer(ctx context.Context, group *inferencev1alpha1.ModelGroup) error {
	base := group.DeepCopy()
	controllerutil.RemoveFinalizer(group, modelGroupDrainFinalizer)
	if err := reconciler.Patch(ctx, group, client.MergeFrom(base)); err != nil {
		return fmt.Errorf("remove ModelGroup drain finalizer: %w", err)
	}
	return nil
}
