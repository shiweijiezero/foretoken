// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Verifies Pod ownership through Deployment and LeaderWorkerSet controller chains.

package workload

import (
	"context"
	"errors"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	lwsv1 "sigs.k8s.io/lws/api/leaderworkerset/v1"
)

var ErrUnprovenBackendOwner = errors.New("backend Pod ownership is not proven")

// ModelGroupForPod proves a new Pod through the Deployment or LWS controller chain.
func ModelGroupForPod(ctx context.Context, reader client.Reader, pod *corev1.Pod) (*inferencev1alpha1.ModelGroup, error) {
	owner := metav1.GetControllerOf(pod)
	if owner == nil {
		return nil, ErrUnprovenBackendOwner
	}
	var executionWorkload client.Object
	switch {
	case owner.APIVersion == appsv1.SchemeGroupVersion.String() && owner.Kind == "ReplicaSet":
		replicaSet := new(appsv1.ReplicaSet)
		if err := readBackendOwner(ctx, reader, pod, replicaSet, appsv1.SchemeGroupVersion.String(), "ReplicaSet"); err != nil {
			return nil, err
		}
		deployment := new(appsv1.Deployment)
		if err := readBackendOwner(ctx, reader, replicaSet, deployment, appsv1.SchemeGroupVersion.String(), "Deployment"); err != nil {
			return nil, err
		}
		executionWorkload = deployment
	case owner.APIVersion == appsv1.SchemeGroupVersion.String() && owner.Kind == "StatefulSet":
		statefulSet := new(appsv1.StatefulSet)
		if err := readBackendOwner(ctx, reader, pod, statefulSet, appsv1.SchemeGroupVersion.String(), "StatefulSet"); err != nil {
			return nil, err
		}
		// LWS worker StatefulSets belong to their leader Pod, not directly to LWS.
		if parent := metav1.GetControllerOf(statefulSet); parent != nil && parent.APIVersion == corev1.SchemeGroupVersion.String() && parent.Kind == "Pod" {
			leader := new(corev1.Pod)
			if err := readBackendOwner(ctx, reader, statefulSet, leader, corev1.SchemeGroupVersion.String(), "Pod"); err != nil {
				return nil, err
			}
			statefulSet = new(appsv1.StatefulSet)
			if err := readBackendOwner(ctx, reader, leader, statefulSet, appsv1.SchemeGroupVersion.String(), "StatefulSet"); err != nil {
				return nil, err
			}
		}
		leaderWorkerSet := new(lwsv1.LeaderWorkerSet)
		if err := readBackendOwner(ctx, reader, statefulSet, leaderWorkerSet, lwsv1.GroupVersion.String(), "LeaderWorkerSet"); err != nil {
			return nil, err
		}
		executionWorkload = leaderWorkerSet
	default:
		return nil, ErrUnprovenBackendOwner
	}
	group := new(inferencev1alpha1.ModelGroup)
	if err := readBackendOwner(ctx, reader, executionWorkload, group, inferencev1alpha1.GroupVersion.String(), "ModelGroup"); err != nil {
		return nil, err
	}
	if pod.Labels[ModelGroupLabel] != group.Name {
		return nil, ErrUnprovenBackendOwner
	}
	return group, nil
}

// readBackendOwner verifies one controller link for a previously unpublished Pod.
func readBackendOwner(ctx context.Context, reader client.Reader, child, parent client.Object, apiVersion, kind string) error {
	owner := metav1.GetControllerOf(child)
	if owner == nil || owner.APIVersion != apiVersion || owner.Kind != kind {
		return ErrUnprovenBackendOwner
	}
	if err := reader.Get(ctx, client.ObjectKey{Namespace: child.GetNamespace(), Name: owner.Name}, parent); err != nil {
		return fmt.Errorf("get backend instance %s owner: %w", kind, err)
	}
	if !ControllerOwnerMatches(child, apiVersion, kind, parent.GetName(), parent.GetUID()) {
		return ErrUnprovenBackendOwner
	}
	return nil
}

// ControllerOwnerMatches verifies the API kind, name, and UID of a resource controller reference.
func ControllerOwnerMatches(object metav1.Object, apiVersion, kind, name string, uid types.UID) bool {
	for _, owner := range object.GetOwnerReferences() {
		if owner.Controller != nil && *owner.Controller && owner.APIVersion == apiVersion && owner.Kind == kind && owner.Name == name && owner.UID == uid {
			return true
		}
	}
	return false
}
