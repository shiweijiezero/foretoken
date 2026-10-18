// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Observes the serving configuration applied by each frontend replica.

package frontend

import (
	"context"
	"fmt"
	"net/http"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/servingconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// observeConfiguration checks every surviving replica owned by the selected Deployment.
// Workload readiness and Gateway acknowledgement remain separate conditions.
func (reconciler *Reconciler) observeConfiguration(ctx context.Context, frontend *inferencev1alpha1.FrontendService, deployment *appsv1.Deployment) metav1.Condition {
	result := metav1.Condition{Status: metav1.ConditionFalse, Reason: "ConfigurationPending", Message: "Waiting for frontend replicas to apply the serving configuration"}
	if deployment == nil {
		return result
	}
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
		// Legacy views share publication versions but omit routes they cannot execute.
		if pod.Annotations[servingconfig.ProtocolAnnotation] != fmt.Sprint(frontend.Status.ServingConfigVersion) {
			return result
		}
		if pod.Status.PodIP == "" {
			return result
		}
		endpoint, err := workload.FrontendPodEndpoint(pod)
		if err != nil {
			return failed(err)
		}
		diagnostics, err := servingconfig.ReadDiagnostics(ctx, http.DefaultClient, endpoint)
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
