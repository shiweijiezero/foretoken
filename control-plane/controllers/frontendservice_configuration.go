// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Observes the configuration actually applied by each frontend workload replica.

package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"slices"
	"time"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// observeConfiguration checks every surviving replica owned by the selected Deployment.
// Workload readiness and Gateway acknowledgement remain separate conditions.
func (reconciler *FrontendServiceReconciler) observeConfiguration(ctx context.Context, frontend *api.FrontendService, deployment *appsv1.Deployment) metav1.Condition {
	result := metav1.Condition{Status: metav1.ConditionFalse, Reason: "ConfigurationPending", Message: "Waiting for frontend replicas to apply the serving configuration"}
	if deployment.Spec.Replicas != nil && *deployment.Spec.Replicas == 0 {
		result.Reason, result.Message = "ScaledToZero", "No frontend replicas are requested"
		return result
	}
	failed := func(err error) metav1.Condition {
		return metav1.Condition{Status: metav1.ConditionUnknown, Reason: "ObservationFailed", Message: err.Error()}
	}
	selector, err := metav1.LabelSelectorAsSelector(deployment.Spec.Selector)
	if err != nil {
		return failed(err)
	}
	// Bound observation, not serving: a slow replica leaves the last active configuration running.
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	// Observe workload membership directly; informer lag must not hide a new replica.
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	owners := make(map[string]*appsv1.ReplicaSet)
	var pods corev1.PodList
	if err := reader.List(ctx, &pods, client.InNamespace(frontend.Namespace), client.MatchingLabelsSelector{Selector: selector}); err != nil {
		return failed(err)
	}
	observed := 0
	for index := range pods.Items {
		pod := &pods.Items[index]
		if !pod.DeletionTimestamp.IsZero() || pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		owner := metav1.GetControllerOf(pod)
		if owner == nil || owner.Kind != "ReplicaSet" {
			continue
		}
		replicaSet := owners[owner.Name]
		if replicaSet == nil {
			replicaSet = new(appsv1.ReplicaSet)
			if err := reader.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: owner.Name}, replicaSet); apierrors.IsNotFound(err) {
				continue
			} else if err != nil {
				return failed(err)
			}
			owners[owner.Name] = replicaSet
		}
		if replicaSet.UID != owner.UID || !metav1.IsControlledBy(replicaSet, deployment) {
			continue
		}
		if pod.Status.PodIP == "" {
			return result
		}
		endpoint, err := frontendPodEndpoint(pod)
		if err != nil {
			return failed(err)
		}
		diagnostics, err := readFrontendDiagnostics(ctx, http.DefaultClient, endpoint)
		if err != nil {
			return failed(fmt.Errorf("observe frontend Pod %q: %w", pod.Name, err))
		}
		if diagnostics.TargetGeneration == nil || *diagnostics.TargetGeneration != frontend.Status.ServingSnapshotVersion {
			return result
		}
		if diagnostics.ConfigurationError != nil {
			result.Reason = "ConfigurationRejected"
			result.Message = fmt.Sprintf("Frontend Pod %q: %s", pod.Name, *diagnostics.ConfigurationError)
			return result
		}
		if diagnostics.ActiveGeneration == nil || *diagnostics.ActiveGeneration != frontend.Status.ServingSnapshotVersion {
			return result
		}
		observed++
	}
	if observed == 0 || deployment.Spec.Replicas != nil && observed < int(*deployment.Spec.Replicas) {
		return result
	}
	return metav1.Condition{Status: metav1.ConditionTrue, Reason: "ConfigurationApplied", Message: "Frontend replicas have applied the serving configuration"}
}

// frontendConfigurationApplied waits for each active frontend's acknowledgement of this service intent.
// The existing ConfigMap identifies the service generation; no second acknowledgement inventory is kept.
func (reconciler *ModelServiceReconciler) frontendConfigurationApplied(ctx context.Context, service *api.ModelService) (bool, string, string, error) {
	reader := reconciler.APIReader
	if reader == nil {
		reader = reconciler.Client
	}
	var frontends api.FrontendServiceList
	if err := reader.List(ctx, &frontends, client.InNamespace(service.Namespace)); err != nil {
		return false, "", "", err
	}
	for index := range frontends.Items {
		frontend := &frontends.Items[index]
		if !frontend.DeletionTimestamp.IsZero() || frontend.Spec.Replicas != nil && *frontend.Spec.Replicas == 0 {
			continue
		}
		pending := fmt.Sprintf("Waiting for frontend %q to apply the model service configuration", frontend.Name)
		configuration := new(corev1.ConfigMap)
		if err := reader.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: frontendServingConfigMapName(frontend)}, configuration); apierrors.IsNotFound(err) {
			return false, "ConfigurationPending", pending, nil
		} else if err != nil {
			return false, "", "", err
		}
		if !metav1.IsControlledBy(configuration, frontend) {
			return false, "ConfigurationPending", pending, nil
		}
		var snapshot servingSnapshot
		if err := json.Unmarshal([]byte(configuration.Data[servingSnapshotKey]), &snapshot); err != nil {
			return false, "", "", fmt.Errorf("decode frontend %q configuration: %w", frontend.Name, err)
		}
		contains := false
		for _, model := range snapshot.Models {
			if model.ServiceUID == string(service.UID) && model.ServiceGeneration == service.Generation && slices.Equal(model.SelectedPoolRevisions, sortedServingPoolRevisions(service.Status.ServingPoolRevisions)) {
				contains = true
				break
			}
		}
		if !contains || frontend.Status.ServingSnapshotVersion != snapshot.Version {
			return false, "ConfigurationPending", pending, nil
		}
		condition := meta.FindStatusCondition(frontend.Status.Conditions, frontendConditionRoutingReady)
		if condition == nil || condition.ObservedGeneration != frontend.Generation || condition.Status != metav1.ConditionTrue || frontend.Status.AppliedServingSnapshotVersion != snapshot.Version {
			if condition != nil && condition.ObservedGeneration == frontend.Generation && condition.Reason == "ConfigurationRejected" {
				return false, condition.Reason, condition.Message, nil
			}
			return false, "ConfigurationPending", pending, nil
		}
	}
	return true, "Ready", "Model instances and frontend configuration are ready", nil
}
