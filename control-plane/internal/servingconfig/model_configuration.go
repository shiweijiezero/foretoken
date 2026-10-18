// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Verifies frontend acknowledgement of a selected model service generation.

package servingconfig

import (
	"context"
	"encoding/json"
	"fmt"
	"slices"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// ModelConfigurationApplied reports whether active frontends have acknowledged the model service's current intent.
// The existing ConfigMap identifies the service generation; no second acknowledgement inventory is kept.
func ModelConfigurationApplied(ctx context.Context, kubeClient client.Client, reader client.Reader, service *inferencev1alpha1.ModelService) (bool, string, string, error) {
	if reader == nil {
		reader = kubeClient
	}
	var frontends inferencev1alpha1.FrontendServiceList
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
		if err := reader.Get(ctx, client.ObjectKey{Namespace: frontend.Namespace, Name: ConfigMapName(frontend, frontend.Status.ServingConfigVersion)}, configuration); apierrors.IsNotFound(err) {
			return false, "ConfigurationPending", pending, nil
		} else if err != nil {
			return false, "", "", err
		}
		if !metav1.IsControlledBy(configuration, frontend) {
			return false, "ConfigurationPending", pending, nil
		}
		var snapshot Snapshot
		if err := json.Unmarshal([]byte(configuration.Data[SnapshotKey]), &snapshot); err != nil {
			return false, "", "", fmt.Errorf("decode frontend %q configuration: %w", frontend.Name, err)
		}
		contains := false
		for _, model := range snapshot.Models {
			if model.ServiceUID == string(service.UID) && model.ServiceGeneration == service.Generation && slices.Equal(model.SelectedPoolRevisions, SortedServingPoolRevisions(service.Status.ServingPoolRevisions)) {
				contains = true
				break
			}
		}
		if !contains || frontend.Status.ServingSnapshotVersion != snapshot.Version {
			return false, "ConfigurationPending", pending, nil
		}
		condition := meta.FindStatusCondition(frontend.Status.Conditions, ConditionRoutingReady)
		if condition == nil || condition.ObservedGeneration != frontend.Generation || condition.Status != metav1.ConditionTrue || frontend.Status.AppliedServingSnapshotVersion != snapshot.Version {
			if condition != nil && condition.ObservedGeneration == frontend.Generation && condition.Reason == "ConfigurationRejected" {
				return false, condition.Reason, condition.Message, nil
			}
			return false, "ConfigurationPending", pending, nil
		}
	}
	return true, "Ready", "Model instances and frontend configuration are ready", nil
}
