// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Publishes proven frontend and backend Pod membership through owner retirement.

package frontend

import (
	"context"
	"errors"
	"fmt"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// frontendInstancePods proves new members and retains published live members across owner deletion.
// Only authoritative Pod absence or a terminal phase removes an already-proven identity.
func (reconciler *Reconciler) frontendInstancePods(ctx context.Context, frontend *inferencev1alpha1.FrontendService, previous []string) ([]corev1.Pod, error) {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	deployment := new(appsv1.Deployment)
	if err := reader.Get(ctx, client.ObjectKeyFromObject(frontend), deployment); err != nil && !apierrors.IsNotFound(err) {
		return nil, fmt.Errorf("get frontend Deployment for instance membership: %w", err)
	}
	deploymentOwned := workload.ControllerOwnerMatches(deployment, inferencev1alpha1.GroupVersion.String(), "FrontendService", frontend.Name, frontend.UID)
	var pods corev1.PodList
	if err := reader.List(ctx, &pods, client.InNamespace(frontend.Namespace)); err != nil {
		return nil, fmt.Errorf("list frontend Pods for instance membership: %w", err)
	}
	replicaSets := make(map[string]*appsv1.ReplicaSet)
	instances := make([]corev1.Pod, 0, len(pods.Items))
	for _, pod := range pods.Items {
		if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		if slices.Contains(previous, string(pod.UID)) {
			instances = append(instances, pod)
			continue
		}
		if !deploymentOwned || pod.Labels[workload.FrontendServiceLabel] != frontend.Name {
			continue
		}
		owner := metav1.GetControllerOf(&pod)
		if owner == nil || owner.APIVersion != appsv1.SchemeGroupVersion.String() || owner.Kind != "ReplicaSet" {
			continue
		}
		replicaSet, found := replicaSets[owner.Name]
		if !found {
			replicaSet = new(appsv1.ReplicaSet)
			if err := reader.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: owner.Name}, replicaSet); err != nil {
				if apierrors.IsNotFound(err) {
					continue
				}
				return nil, fmt.Errorf("get frontend ReplicaSet for instance membership: %w", err)
			}
			replicaSets[owner.Name] = replicaSet
		}
		if workload.ControllerOwnerMatches(replicaSet, appsv1.SchemeGroupVersion.String(), "Deployment", deployment.Name, deployment.UID) &&
			workload.ControllerOwnerMatches(&pod, appsv1.SchemeGroupVersion.String(), "ReplicaSet", replicaSet.Name, replicaSet.UID) {
			instances = append(instances, pod)
		}
	}
	return instances, nil
}

// backendInstances proves new workload members and retains published live Pods across owner deletion.
// Draining cohorts and LWS workers remain members until their Pod disappears or becomes terminal.
func (reconciler *Reconciler) backendInstances(ctx context.Context, namespace string, previous []string) ([]string, error) {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var pods corev1.PodList
	if err := reader.List(ctx, &pods, client.InNamespace(namespace)); err != nil {
		return nil, fmt.Errorf("list backend Pods for instance membership: %w", err)
	}
	instances := make([]string, 0, len(pods.Items))
	for index := range pods.Items {
		pod := &pods.Items[index]
		if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		if !slices.Contains(previous, string(pod.UID)) {
			if pod.Labels[workload.ModelGroupLabel] == "" {
				continue
			}
			if _, err := workload.ModelGroupForPod(ctx, reader, pod); err != nil {
				if apierrors.IsNotFound(err) || errors.Is(err, workload.ErrUnprovenBackendOwner) {
					continue
				}
				return nil, err
			}
		}
		instances = append(instances, string(pod.UID))
	}
	slices.Sort(instances)
	return instances, nil
}
