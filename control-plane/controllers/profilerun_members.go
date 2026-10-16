// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Retains selected engine-container exit observations across Pod deletion.
package controllers

import (
	"context"
	"fmt"
	"slices"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// profileMembers preserves Pod-level recovery for plans created without container identities.
func profileMembers(plan *api.ProfileExecutionPlan) []api.ProfileMember {
	if len(plan.Members) > 0 {
		return plan.Members
	}
	members := make([]api.ProfileMember, 0, len(plan.Participants))
	for _, participant := range plan.Participants {
		members = append(members, api.ProfileMember{
			GroupUID: participant.GroupUID, PodName: participant.PodName, PodUID: participant.PodUID,
		})
	}
	return members
}

// protectProfileMembers retains every selected Pod before any native start can be accepted.
// Pending runs have not issued native operations and can finish immediately if selection changed.
func (r *ProfileRunReconciler) protectProfileMembers(ctx context.Context, run *api.ProfileRun) error {
	if run.Spec.Action != "Capture" || !run.DeletionTimestamp.IsZero() || run.Status.Reason != "" {
		run.Status.Phase = "Cancelled"
		if run.Status.Reason != "" {
			run.Status.Phase = "Failed"
		}
		return r.writeProfileStatus(ctx, run)
	}
	for _, member := range profileMembers(run.Status.Plan) {
		pod := new(corev1.Pod)
		err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: member.PodName}, pod)
		if err != nil && !apierrors.IsNotFound(err) {
			return err
		}
		current := false
		for _, container := range pod.Status.ContainerStatuses {
			if container.Name == member.ContainerName && container.ContainerID == member.ContainerID && container.State.Running != nil {
				current = true
			}
		}
		if apierrors.IsNotFound(err) || string(pod.UID) != member.PodUID || !pod.DeletionTimestamp.IsZero() || !podReady(pod) || !current {
			run.Status.Phase, run.Status.Reason, run.Status.Message = "Failed", "TargetUnavailable", "selected engine container changed before capture started"
			return r.writeProfileStatus(ctx, run)
		}
		finalizer := "inference.foretoken.io/profile-" + string(run.UID)
		if !controllerutil.ContainsFinalizer(pod, finalizer) {
			base := pod.DeepCopy()
			controllerutil.AddFinalizer(pod, finalizer)
			if err := r.Patch(ctx, pod, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
				return fmt.Errorf("retain profiling member %s: %w", pod.Name, err)
			}
		}
	}
	run.Status.Phase = "Starting"
	return r.writeProfileStatus(ctx, run)
}

// observeProfileMembers persists exits before releasing each Pod's evidence protection.
// Releasing members individually lets background deletion of an LWS leader reach its workers.
func (r *ProfileRunReconciler) observeProfileMembers(ctx context.Context, run *api.ProfileRun) (map[string]*corev1.Pod, error) {
	pods := make(map[string]*corev1.Pod)
	changed := false
	for _, member := range profileMembers(run.Status.Plan) {
		pod := new(corev1.Pod)
		err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: member.PodName}, pod)
		if err != nil && !apierrors.IsNotFound(err) {
			return nil, err
		}
		if apierrors.IsNotFound(err) || string(pod.UID) != member.PodUID {
			if !slices.Contains(run.Status.StoppedMembers, member.PodUID) && run.Status.Reason == "" {
				run.Status.Reason, run.Status.Message = "StopUnconfirmed", "a selected Pod disappeared before its container exit was observed"
				changed = true
			}
			continue
		}
		if !pod.DeletionTimestamp.IsZero() && run.Status.Reason == "" {
			run.Status.Reason, run.Status.Message = "CoverageChanged", "a selected Pod is terminating"
			changed = true
		}
		pods[member.PodUID] = pod
		if profileContainerStopped(member, pod) && !slices.Contains(run.Status.StoppedMembers, member.PodUID) {
			run.Status.StoppedMembers = append(run.Status.StoppedMembers, member.PodUID)
			changed = true
		}
	}
	if len(run.Status.StoppedMembers) > 0 && run.Status.Reason == "" {
		run.Status.Reason, run.Status.Message = "RuntimeLost", "a selected engine container stopped during capture"
		changed = true
	}
	if changed {
		if err := r.writeProfileStatus(ctx, run); err != nil {
			return nil, err
		}
	}
	for _, uid := range run.Status.StoppedMembers {
		if pod := pods[uid]; pod != nil {
			if err := r.releaseProfileMember(ctx, run, pod); err != nil {
				return nil, err
			}
		}
	}
	return pods, nil
}

// profileContainerStopped observes the fixed container, rather than treating contact loss as exit.
func profileContainerStopped(member api.ProfileMember, pod *corev1.Pod) bool {
	if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
		return true
	}
	if member.ContainerID == "" {
		return false
	}
	for _, container := range pod.Status.ContainerStatuses {
		if container.Name != member.ContainerName {
			continue
		}
		if container.ContainerID == member.ContainerID && container.State.Terminated != nil {
			return true
		}
		if previous := container.LastTerminationState.Terminated; previous != nil && previous.ContainerID == member.ContainerID {
			return true
		}
		// Kubelet replaces a named container only after its previous instance stops.
		// Container IDs remain distinct even when node recovery resets RestartCount.
		return container.ContainerID != "" && container.ContainerID != member.ContainerID
	}
	return false
}

// profileParticipantStopped covers the entire RPC-controlled Group or one local Nsight session.
func profileParticipantStopped(run *api.ProfileRun, participant api.ProfileParticipant) bool {
	covered := false
	for _, member := range profileMembers(run.Status.Plan) {
		if member.GroupUID != participant.GroupUID || (run.Spec.Engine == "nsight" && member.PodUID != participant.PodUID) {
			continue
		}
		covered = true
		if !slices.Contains(run.Status.StoppedMembers, member.PodUID) {
			return false
		}
	}
	return covered
}

// releaseProfileMember removes only this run's protection after durable stop or run completion.
func (r *ProfileRunReconciler) releaseProfileMember(ctx context.Context, run *api.ProfileRun, pod *corev1.Pod) error {
	finalizer := "inference.foretoken.io/profile-" + string(run.UID)
	if !controllerutil.ContainsFinalizer(pod, finalizer) {
		return nil
	}
	base := pod.DeepCopy()
	controllerutil.RemoveFinalizer(pod, finalizer)
	return r.Patch(ctx, pod, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
}

// releaseProfileMembers finishes cleanup after a durable terminal result, including failed preparation.
func (r *ProfileRunReconciler) releaseProfileMembers(ctx context.Context, run *api.ProfileRun) error {
	if run.Status.Plan == nil {
		return nil
	}
	for _, member := range profileMembers(run.Status.Plan) {
		pod := new(corev1.Pod)
		if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: run.Namespace, Name: member.PodName}, pod); apierrors.IsNotFound(err) {
			continue
		} else if err != nil {
			return err
		}
		if string(pod.UID) == member.PodUID {
			if err := r.releaseProfileMember(ctx, run, pod); err != nil {
				return err
			}
		}
	}
	return nil
}
