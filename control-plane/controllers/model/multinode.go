// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Materializes complete model groups through Deployments or LeaderWorkerSets.

package model

import (
	"context"
	"errors"
	"fmt"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

// modelGroupWorkloadState reports persisted workload identity and its controller's availability.
type modelGroupWorkloadState struct {
	uid       types.UID
	available bool
}

// reconcileWorkload returns the persisted workload identity and native controller availability.
func (reconciler *GroupReconciler) reconcileWorkload(ctx context.Context, group *inferencev1alpha1.ModelGroup, deployment *appsv1.Deployment) (modelGroupWorkloadState, error) {
	if group.Spec.NodeCount == 1 {
		deployment, err := reconciler.reconcileDeployment(ctx, group, deployment)
		if err != nil {
			return modelGroupWorkloadState{}, err
		}
		return modelGroupWorkloadState{uid: deployment.UID, available: modelGroupDeploymentAvailable(deployment)}, nil
	}
	workloadName := workload.ModelGroupLeaderWorkerSetName(group)
	if workloadName != group.Name {
		// Foreground deletion keeps the old group from holding GPUs when its
		// replacement starts, including when LWS never created every member.
		previous := new(lwsv1.LeaderWorkerSet)
		err := reconciler.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: group.Name}, previous)
		if err == nil {
			if !metav1.IsControlledBy(previous, group) {
				return modelGroupWorkloadState{}, fmt.Errorf("LeaderWorkerSet %q is not controlled by ModelGroup", previous.Name)
			}
			if previous.DeletionTimestamp.IsZero() {
				if err := reconciler.Delete(ctx, previous, client.PropagationPolicy(metav1.DeletePropagationForeground), client.Preconditions{UID: &previous.UID}); client.IgnoreNotFound(err) != nil {
					return modelGroupWorkloadState{}, fmt.Errorf("delete superseded LeaderWorkerSet: %w", err)
				}
			}
			return modelGroupWorkloadState{}, nil
		}
		if !apierrors.IsNotFound(err) {
			return modelGroupWorkloadState{}, fmt.Errorf("get superseded LeaderWorkerSet: %w", err)
		}
	}
	member := deployment.Spec.Template
	if member.Spec.Affinity == nil {
		member.Spec.Affinity = &corev1.Affinity{}
	}
	if member.Spec.Affinity.PodAntiAffinity == nil {
		member.Spec.Affinity.PodAntiAffinity = &corev1.PodAntiAffinity{}
	}
	member.Spec.Affinity.PodAntiAffinity.RequiredDuringSchedulingIgnoredDuringExecution = append(
		member.Spec.Affinity.PodAntiAffinity.RequiredDuringSchedulingIgnoredDuringExecution,
		corev1.PodAffinityTerm{
			LabelSelector: &metav1.LabelSelector{MatchLabels: workload.ModelGroupLabels(group)},
			TopologyKey:   corev1.LabelHostname,
		},
	)
	if err := runtimeconfig.PlaceRuntimeCache(ctx, reconciler.Client, group.Namespace, group.Spec.Artifacts.Cache, &member); err != nil {
		return modelGroupWorkloadState{}, err
	}
	leader := member.DeepCopy()
	if groupUsesLiveInstanceAdmission(group) {
		// Only the leader owns HTTP ingress admission; workers prove local process readiness.
		member.Spec.Volumes = slices.DeleteFunc(member.Spec.Volumes, func(volume corev1.Volume) bool { return volume.Name == instanceAdmissionVolumeName })
		container := &member.Spec.Containers[0]
		container.VolumeMounts = slices.DeleteFunc(container.VolumeMounts, func(mount corev1.VolumeMount) bool { return mount.Name == instanceAdmissionVolumeName })
		container.Env = slices.DeleteFunc(container.Env, func(env corev1.EnvVar) bool { return env.Name == instanceAdmissionConfigEnv })
		container.ReadinessProbe.HTTPGet.Path = "/readyz"
	}
	one := int32(1)
	desired := &lwsv1.LeaderWorkerSet{
		TypeMeta:   metav1.TypeMeta{APIVersion: lwsv1.GroupVersion.String(), Kind: "LeaderWorkerSet"},
		ObjectMeta: metav1.ObjectMeta{Name: workloadName, Namespace: group.Namespace, Labels: workload.ModelGroupLabels(group)},
		Spec: lwsv1.LeaderWorkerSetSpec{
			Replicas:      &one,
			StartupPolicy: lwsv1.LeaderCreatedStartupPolicy,
			LeaderWorkerTemplate: lwsv1.LeaderWorkerTemplate{
				Size:           &group.Spec.MemberCount,
				LeaderTemplate: leader,
				WorkerTemplate: member,
				RestartPolicy:  lwsv1.RecreateGroupOnPodRestart,
			},
		},
	}
	configureLeaderWorkerScheduling(desired, group.Spec.Runtime.LeaderWorkerScheduling)
	if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
		return modelGroupWorkloadState{}, fmt.Errorf("set LeaderWorkerSet owner: %w", err)
	}
	current := new(lwsv1.LeaderWorkerSet)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); err == nil {
		if !metav1.IsControlledBy(current, group) {
			return modelGroupWorkloadState{}, fmt.Errorf("LeaderWorkerSet %q is not controlled by ModelGroup", current.Name)
		}
	} else if !apierrors.IsNotFound(err) {
		return modelGroupWorkloadState{}, fmt.Errorf("get LeaderWorkerSet: %w", err)
	}
	// An older cohort keeps its original Pod template while the Pool rolls out the new contract.
	if group.Spec.Runtime.PreparationVersion > 0 || current.ResourceVersion == "" {
		if err := reconciler.requireLeaderWorkerQueueAdmission(ctx, desired, group.Spec.Runtime.LeaderWorkerScheduling); err != nil {
			return modelGroupWorkloadState{uid: current.UID}, err
		}
		if err := reconciler.Patch(ctx, desired, client.Apply, client.FieldOwner(modelGroupFieldOwner), client.ForceOwnership); err != nil {
			return modelGroupWorkloadState{}, fmt.Errorf("apply LeaderWorkerSet: %w", err)
		}
		current = desired
	}
	return modelGroupWorkloadState{
		uid: current.UID,
		available: current.Status.ObservedGeneration == current.Generation && current.Status.ReadyReplicas == 1 &&
			meta.IsStatusConditionTrue(current.Status.Conditions, string(lwsv1.LeaderWorkerSetAvailable)),
	}, nil
}

// currentWorkloadPods returns proven members of the selected execution workload for status observation.
func (reconciler *GroupReconciler) currentWorkloadPods(ctx context.Context, group *inferencev1alpha1.ModelGroup, uid types.UID) ([]corev1.Pod, error) {
	if uid == "" {
		return nil, nil
	}
	var candidates corev1.PodList
	if err := reconciler.List(ctx, &candidates, client.InNamespace(group.Namespace), client.MatchingLabels(workload.ModelGroupLabels(group))); err != nil {
		return nil, fmt.Errorf("observe Group Pods: %w", err)
	}
	// Stable Group labels also match Pods retiring from a replaced workload.
	pods := make([]corev1.Pod, 0, len(candidates.Items))
	for index := range candidates.Items {
		pod := &candidates.Items[index]
		owner, err := workload.ModelGroupForPod(ctx, reconciler.APIReader, pod)
		if apierrors.IsNotFound(err) || errors.Is(err, workload.ErrUnprovenBackendOwner) {
			continue
		}
		if err != nil {
			return nil, err
		}
		if owner.Workload.GetUID() == uid {
			pods = append(pods, *pod)
		}
	}
	return pods, nil
}
