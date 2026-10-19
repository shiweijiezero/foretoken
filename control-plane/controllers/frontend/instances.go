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
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
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

// frontendsForPod wakes only affected frontends; unpublished Pods are never recognized by provenance.
// Unlabelled terminal and delete events use the published UID lists instead of broadcasting ordinary Pod updates.
func (reconciler *Reconciler) frontendsForPod(ctx context.Context, object client.Object) []reconcile.Request {
	if name := object.GetLabels()[workload.FrontendServiceLabel]; name != "" {
		return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: object.GetNamespace(), Name: name}}}
	}
	if object.GetLabels()[workload.ModelGroupLabel] != "" {
		return reconciler.frontendsInNamespace(ctx, object)
	}
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var configurations corev1.ConfigMapList
	if err := reader.List(ctx, &configurations, client.InNamespace(object.GetNamespace()), client.HasLabels{workload.FrontendServiceLabel}); err != nil {
		ctrl.LoggerFrom(ctx).Error(err, "read published instance membership for Pod termination")
		return reconciler.frontendsInNamespace(ctx, object)
	}
	requests := make([]reconcile.Request, 0)
	for index := range configurations.Items {
		configuration := &configurations.Items[index]
		owner := metav1.GetControllerOf(configuration)
		if owner == nil || owner.APIVersion != inferencev1alpha1.GroupVersion.String() || owner.Kind != "FrontendService" {
			continue
		}
		payload, exists := configuration.Data[servingconfig.SnapshotKey]
		if !exists {
			continue
		}
		snapshot, _, err := readServingPublication([]byte(payload))
		if err != nil {
			ctrl.LoggerFrom(ctx).Error(err, "read published instance membership", "configMap", configuration.Name)
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKey{Namespace: configuration.Namespace, Name: owner.Name}})
			continue
		}
		uid := string(object.GetUID())
		if !slices.Contains(snapshot.FrontendInstances, uid) && !slices.Contains(snapshot.BackendInstances, uid) {
			continue
		}
		frontend := new(inferencev1alpha1.FrontendService)
		if err := reader.Get(ctx, client.ObjectKey{Namespace: configuration.Namespace, Name: owner.Name}, frontend); apierrors.IsNotFound(err) {
			continue
		} else if err != nil {
			ctrl.LoggerFrom(ctx).Error(err, "read frontend for published instance membership")
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKey{Namespace: configuration.Namespace, Name: owner.Name}})
			continue
		}
		if slices.ContainsFunc(servingconfig.ProtocolVersions(frontend), func(version uint32) bool {
			return configuration.Name == servingconfig.ConfigMapName(frontend, version)
		}) && workload.ControllerOwnerMatches(configuration, inferencev1alpha1.GroupVersion.String(), "FrontendService", frontend.Name, frontend.UID) {
			requests = append(requests, reconcile.Request{NamespacedName: client.ObjectKeyFromObject(frontend)})
		}
	}
	return requests
}
