// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles diagnostic capture intent against a persisted, fixed set of runtimes.
package controllers

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"reflect"
	"strconv"
	"time"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const profileFinalizer = "inference.foretoken.io/profile-stop"
const profilePath = "/v1/internal/profiling"

// ProfileRunReconciler owns capture coordination, never serving resources or artifact storage.
type ProfileRunReconciler struct {
	client.Client
	APIReader  client.Reader
	HTTPClient *http.Client
}

type profileRecord struct {
	RunUID          string  `json:"runUid"`
	Phase           string  `json:"phase"`
	StartedAtUnixMS *int64  `json:"startedAtUnixMs"`
	ArtifactDir     *string `json:"artifactDir"`
	Message         string  `json:"message"`
	GPUActivity     *bool   `json:"gpuActivity"`
}

type profileObservation struct {
	RuntimeID     string         `json:"runtimeId"`
	PodUID        string         `json:"podUid"`
	GroupUID      string         `json:"groupUid"`
	ArtifactClaim string         `json:"artifactClaim"`
	Capture       *profileRecord `json:"capture"`
}

// SetupWithManager registers the existing manager as the sole capture coordinator.
func (r *ProfileRunReconciler) SetupWithManager(manager ctrl.Manager) error {
	r.APIReader = manager.GetAPIReader()
	r.HTTPClient = &http.Client{Timeout: 5 * time.Second}
	return ctrl.NewControllerManagedBy(manager).For(&api.ProfileRun{}).Complete(r)
}

// Reconcile persists recovery state before starting work, then observes the same runtime identities.
func (r *ProfileRunReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	run := new(api.ProfileRun)
	if err := r.APIReader.Get(ctx, request.NamespacedName, run); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if profileTerminal(run.Status.Phase) {
		return ctrl.Result{}, r.removeProfileFinalizer(ctx, run)
	}
	if run.Status.Plan == nil {
		if run.Status.Phase != "" {
			return ctrl.Result{}, fmt.Errorf("capture recovery plan is missing; refusing to reselect runtimes")
		}
		if run.Spec.Action != "Capture" || !run.DeletionTimestamp.IsZero() {
			run.Status.Phase = "Cancelled"
			return ctrl.Result{}, r.writeProfileStatus(ctx, run)
		}
		if !controllerutil.ContainsFinalizer(run, profileFinalizer) {
			base := run.DeepCopy()
			controllerutil.AddFinalizer(run, profileFinalizer)
			return ctrl.Result{Requeue: true}, r.Patch(ctx, run, client.MergeFrom(base))
		}
		plan, err := r.prepareProfile(ctx, run)
		if err != nil {
			run.Status.Phase, run.Status.Reason, run.Status.Message = "Failed", "TargetUnavailable", err.Error()
			return ctrl.Result{}, r.writeProfileStatus(ctx, run)
		}
		run.Status.Plan = &plan
		run.Status.Phase = "Starting"
		// Persist participant identities before any native operation can be accepted.
		return ctrl.Result{Requeue: true}, r.writeProfileStatus(ctx, run)
	}
	plan := *run.Status.Plan

	action := run.Spec.Action
	if !run.DeletionTimestamp.IsZero() || run.Status.Reason != "" {
		action = "Cancel"
	}
	service := new(api.ModelService)
	err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: run.Spec.ModelServiceRef.Name}, service)
	if err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	if apierrors.IsNotFound(err) || string(service.UID) != plan.ServiceUID || service.Status.ServingGeneration != plan.ServingGeneration || !reflect.DeepEqual(service.Status.ServingPoolRevisions, plan.Revisions) || !service.DeletionTimestamp.IsZero() {
		if run.Status.Reason == "" {
			run.Status.Reason, run.Status.Message = "CoverageChanged", "selected serving generation is no longer active"
		}
		action = "Cancel"
	}
	unchanged, err := r.profileCohortUnchanged(ctx, run.Namespace, plan)
	if err != nil {
		return ctrl.Result{}, err
	}
	if !unchanged {
		if run.Status.Reason == "" {
			run.Status.Reason, run.Status.Message = "CoverageChanged", "selected runtime cohort changed during capture"
		}
		action = "Cancel"
	}
	duration, err := time.ParseDuration(string(run.Spec.Duration))
	if err != nil || duration < time.Millisecond {
		return ctrl.Result{}, fmt.Errorf("capture duration must be at least 1ms: %q", run.Spec.Duration)
	}
	allDone, anyStopping := true, false
	capturing := 0
	completed := int32(0)
	gpuActive := 0
	hasArtifacts := false
	for _, participant := range plan.Participants {
		pod := new(corev1.Pod)
		err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: participant.PodName}, pod)
		if apierrors.IsNotFound(err) || (err == nil && string(pod.UID) != participant.PodUID) {
			allDone = false
			if run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "StopUnconfirmed", "selected Pod is missing; engine stop cannot be confirmed"
			}
			action = "Cancel"
			continue
		}
		if err == nil && (pod.Status.Phase == corev1.PodFailed || pod.Status.Phase == corev1.PodSucceeded) {
			if run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "RuntimeLost", "a selected runtime was removed or terminated"
			}
			action = "Cancel"
			continue
		}
		if err != nil {
			return ctrl.Result{}, err
		}
		observation, err := r.profileHTTP(ctx, participant.Endpoint, string(run.UID), nil)
		if err != nil {
			// Loss of contact is not evidence that an engine stopped. Keep the finalizer and plan.
			allDone = false
			if run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "RuntimeUnreachable", err.Error()
			}
			action = "Cancel"
			continue
		}
		if observation.RuntimeID != participant.RuntimeID || observation.PodUID != participant.PodUID || observation.GroupUID != participant.GroupUID {
			if run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "RuntimeReplaced", "the selected engine process was replaced"
			}
			action = "Cancel"
			continue
		}
		if observation.Capture == nil || (!profileTerminal(observation.Capture.Phase) && action != "Capture") {
			operation := struct {
				RunUID     string `json:"runUid"`
				RuntimeID  string `json:"runtimeId"`
				GroupUID   string `json:"groupUid"`
				Action     string `json:"action"`
				DurationMS int64  `json:"durationMs"`
			}{string(run.UID), participant.RuntimeID, participant.GroupUID, action, duration.Milliseconds()}
			observation, err = r.profileHTTP(ctx, participant.Endpoint, string(run.UID), operation)
			if err != nil {
				allDone = false
				if run.Status.Reason == "" {
					run.Status.Reason, run.Status.Message = "ControlFailed", err.Error()
				}
				action = "Cancel"
				continue
			}
		}
		record := observation.Capture
		if record == nil || record.RunUID != string(run.UID) {
			return ctrl.Result{}, fmt.Errorf("runtime returned an invalid capture identity")
		}
		if record.ArtifactDir != nil && *record.ArtifactDir == "runs/"+string(run.UID)+"/"+participant.RuntimeID {
			hasArtifacts = true
		}
		if record.StartedAtUnixMS != nil {
			started := metav1.NewTime(time.UnixMilli(*record.StartedAtUnixMS))
			if run.Status.StartedAt == nil || started.Before(run.Status.StartedAt) {
				run.Status.StartedAt = &started
			}
		}
		switch record.Phase {
		case "Succeeded":
			if record.ArtifactDir == nil || *record.ArtifactDir != "runs/"+string(run.UID)+"/"+participant.RuntimeID {
				if run.Status.Reason == "" {
					run.Status.Reason, run.Status.Message = "InvalidArtifact", "runtime did not publish the expected artifact reference"
				}
				action = "Cancel"
			} else {
				completed++
				if record.GPUActivity != nil && *record.GPUActivity {
					gpuActive++
				}
			}
		case "Cancelled", "Failed":
			if record.Phase == "Failed" && run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "CaptureFailed", record.Message
			}
			action = "Cancel"
		default:
			allDone = false
			if record.Phase == "Capturing" {
				capturing++
			}
			anyStopping = anyStopping || record.Phase == "Stopping"
		}
	}
	run.Status.Participants, run.Status.CompletedParticipants = int32(len(plan.Participants)), completed
	run.Status.Phase = "Starting"
	if capturing == len(plan.Participants) {
		run.Status.Phase = "Capturing"
	}
	if anyStopping || action != "Capture" {
		run.Status.Phase = "Stopping"
	}
	if allDone {
		now := metav1.Now()
		run.Status.FinishedAt = &now
		switch {
		case run.Status.Reason != "":
			run.Status.Phase = "Failed"
		case action == "Cancel":
			run.Status.Phase = "Cancelled"
		default:
			run.Status.Phase = "Succeeded"
			run.Status.Message = fmt.Sprintf("GPU kernel activity recorded on %d/%d runtimes", gpuActive, len(plan.Participants))
		}
		if hasArtifacts {
			run.Status.Artifact = &api.ProfileArtifactReference{ClaimName: plan.ArtifactClaim, Path: "runs/" + string(run.UID)}
		}
	}
	return ctrl.Result{RequeueAfter: time.Second}, r.writeProfileStatus(ctx, run)
}

// profileCohortUnchanged detects scale/replacement changes without retargeting the fixed execution plan.
func (r *ProfileRunReconciler) profileCohortUnchanged(ctx context.Context, namespace string, plan api.ProfileExecutionPlan) (bool, error) {
	groups := new(api.ModelGroupList)
	if err := r.APIReader.List(ctx, groups, client.InNamespace(namespace)); err != nil {
		return false, err
	}
	current := map[string]bool{}
	for _, group := range groups.Items {
		for _, revision := range plan.Revisions {
			if group.Spec.ModelPoolRef.UID == revision.PoolUID && group.Spec.Revision == revision.Revision && routingGroupReady(&group) {
				current[string(group.UID)] = true
			}
		}
	}
	expected := map[string]bool{}
	for _, participant := range plan.Participants {
		expected[participant.GroupUID] = true
	}
	return reflect.DeepEqual(current, expected), nil
}

// prepareProfile resolves the committed serving cohort without introducing a second routing policy.
func (r *ProfileRunReconciler) prepareProfile(ctx context.Context, run *api.ProfileRun) (api.ProfileExecutionPlan, error) {
	var plan api.ProfileExecutionPlan
	service := new(api.ModelService)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: run.Spec.ModelServiceRef.Name}, service); err != nil {
		return plan, err
	}
	if !modelServiceReady(service) || !meta.IsStatusConditionTrue(service.Status.Conditions, conditionReady) {
		return plan, fmt.Errorf("ModelService must already be Ready")
	}
	plan.ServiceUID, plan.ServingGeneration, plan.Revisions = string(service.UID), service.Status.ServingGeneration, service.Status.ServingPoolRevisions
	pools, groups := new(api.ModelPoolList), new(api.ModelGroupList)
	if err := r.APIReader.List(ctx, pools, client.InNamespace(run.Namespace)); err != nil {
		return plan, err
	}
	if err := r.APIReader.List(ctx, groups, client.InNamespace(run.Namespace)); err != nil {
		return plan, err
	}
	coveredPools := 0
	for _, pool := range pools.Items {
		if !routingPoolOwnedBy(&pool, service) || serviceServingRevision(service, &pool) == "" {
			continue
		}
		before := len(plan.Participants)
		for _, group := range groups.Items {
			if !routingGroupOwnedBy(&group, &pool) || group.Spec.Revision != serviceServingRevision(service, &pool) || !routingGroupReady(&group) {
				continue
			}
			pods := new(corev1.PodList)
			if err := r.APIReader.List(ctx, pods, client.InNamespace(run.Namespace), client.MatchingLabels{modelGroupLabel: group.Name}); err != nil {
				return plan, err
			}
			selected := 0
			for _, pod := range pods.Items {
				if !podReady(&pod) || !pod.DeletionTimestamp.IsZero() {
					continue
				}
				if err := r.verifyProfilePod(ctx, &pod, &group); err != nil {
					return plan, err
				}
				endpoint := "http://" + net.JoinHostPort(pod.Status.PodIP, strconv.Itoa(int(group.Spec.Runtime.Port)))
				observation, err := r.profileHTTP(ctx, endpoint, "", nil)
				if err != nil {
					return plan, fmt.Errorf("ModelGroup %s is not prepared for profiling: %w", group.Name, err)
				}
				if observation.GroupUID != string(group.UID) || observation.PodUID != string(pod.UID) || observation.RuntimeID == "" || observation.ArtifactClaim == "" {
					return plan, fmt.Errorf("runtime returned incomplete diagnostic identity")
				}
				if plan.ArtifactClaim != "" && plan.ArtifactClaim != observation.ArtifactClaim {
					return plan, fmt.Errorf("diagnostic runtimes must share the namespace artifact claim")
				}
				plan.ArtifactClaim = observation.ArtifactClaim
				plan.Participants = append(plan.Participants, api.ProfileParticipant{GroupName: group.Name, GroupUID: string(group.UID), PodName: pod.Name, PodUID: string(pod.UID), RuntimeID: observation.RuntimeID, Endpoint: endpoint})
				selected++
			}
			if selected != int(group.Spec.MemberCount) {
				return plan, fmt.Errorf("ModelGroup %s does not have its expected Ready runtime members", group.Name)
			}
		}
		if len(plan.Participants) == before {
			return plan, fmt.Errorf("serving ModelPool %s has no Ready diagnostic runtimes", pool.Name)
		}
		coveredPools++
	}
	if coveredPools != len(plan.Revisions) || len(plan.Participants) == 0 {
		return plan, fmt.Errorf("not every committed serving pool has Ready diagnostic runtimes")
	}
	return plan, nil
}

// verifyProfilePod checks the complete ownership chain rather than trusting a Pod label.
func (r *ProfileRunReconciler) verifyProfilePod(ctx context.Context, pod *corev1.Pod, group *api.ModelGroup) error {
	owner := metav1.GetControllerOf(pod)
	if owner == nil || owner.Kind != "ReplicaSet" {
		return fmt.Errorf("diagnostic Pod is not owned by a ReplicaSet")
	}
	replicaSet := new(appsv1.ReplicaSet)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: pod.Namespace, Name: owner.Name}, replicaSet); err != nil {
		return err
	}
	deployment := new(appsv1.Deployment)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: group.Name}, deployment); err != nil {
		return err
	}
	if !metav1.IsControlledBy(pod, replicaSet) || !metav1.IsControlledBy(replicaSet, deployment) || !metav1.IsControlledBy(deployment, group) {
		return fmt.Errorf("diagnostic Pod ownership does not match ModelGroup")
	}
	return nil
}

// profileHTTP uses the existing internal listener; write responses acknowledge acceptance, not completion.
func (r *ProfileRunReconciler) profileHTTP(ctx context.Context, endpoint, uid string, operation any) (profileObservation, error) {
	var observation profileObservation
	method := http.MethodGet
	var body io.Reader
	if operation != nil {
		method = http.MethodPost
		data, err := json.Marshal(operation)
		if err != nil {
			return observation, err
		}
		body = bytes.NewReader(data)
	}
	request, err := http.NewRequestWithContext(ctx, method, endpoint+profilePath+"?run_uid="+uid, body)
	if err != nil {
		return observation, err
	}
	request.Header.Set("Content-Type", "application/json")
	response, err := r.HTTPClient.Do(request)
	if err != nil {
		return observation, err
	}
	defer response.Body.Close()
	if response.StatusCode != http.StatusOK && response.StatusCode != http.StatusAccepted {
		return observation, fmt.Errorf("profiling endpoint returned HTTP %d", response.StatusCode)
	}
	err = json.NewDecoder(response.Body).Decode(&observation)
	return observation, err
}

func profileTerminal(phase string) bool {
	return phase == "Succeeded" || phase == "Failed" || phase == "Cancelled"
}

// writeProfileStatus publishes only observed progress while preserving user-owned intent.
func (r *ProfileRunReconciler) writeProfileStatus(ctx context.Context, run *api.ProfileRun) error {
	run.Status.ObservedGeneration = run.Generation
	return r.Status().Update(ctx, run)
}

// removeProfileFinalizer releases the recovery plan only after all possible starts have stopped.
func (r *ProfileRunReconciler) removeProfileFinalizer(ctx context.Context, run *api.ProfileRun) error {
	if !controllerutil.ContainsFinalizer(run, profileFinalizer) {
		return nil
	}
	base := run.DeepCopy()
	controllerutil.RemoveFinalizer(run, profileFinalizer)
	return r.Patch(ctx, run, client.MergeFrom(base))
}
