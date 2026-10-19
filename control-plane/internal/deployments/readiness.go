// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package deployments

import (
	"context"
	"fmt"
	"strings"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	coordinationv1 "k8s.io/api/coordination/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type leaderObservation struct {
	controller string
	holder     string
	container  string
	renewal    string
	confirmed  bool
}

// leaderReady observes renewal by a live controller after rollout. Standby Pods
// remain ready so rolling updates can terminate the former leader without deadlocking.
func (r *Reconciler) leaderReady(ctx context.Context, deployment *api.InferenceDeployment, controller *appsv1.Deployment) (bool, error) {
	container, err := managerContainer(controller)
	if err != nil {
		return false, err
	}
	if controllerArgument(container, "leader-elect") != "true" {
		return true, nil
	}
	lease := new(coordinationv1.Lease)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: deployment.Namespace, Name: ResourceName(string(deployment.UID))}, lease); err != nil {
		return false, client.IgnoreNotFound(err)
	}
	if lease.Spec.HolderIdentity == nil || lease.Spec.RenewTime == nil {
		return false, nil
	}
	holder := *lease.Spec.HolderIdentity
	separator := strings.LastIndexByte(holder, '_')
	if separator <= 0 {
		return false, nil
	}
	pod := new(corev1.Pod)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: deployment.Namespace, Name: holder[:separator]}, pod); apierrors.IsNotFound(err) {
		return false, nil
	} else if err != nil {
		return false, err
	}
	if !pod.DeletionTimestamp.IsZero() || pod.Labels[api.DeploymentUIDLabel] != string(deployment.UID) {
		return false, nil
	}
	for _, status := range pod.Status.ContainerStatuses {
		if status.Name != "manager" || !status.Ready || status.State.Running == nil || status.ContainerID == "" {
			continue
		}
		current := leaderObservation{controller: fmt.Sprintf("%s:%d", controller.UID, controller.Generation), holder: holder, container: status.ContainerID, renewal: lease.Spec.RenewTime.String()}
		if previous, observed := r.leaders.Load(deployment.UID); observed {
			last := previous.(leaderObservation)
			current.confirmed = last.controller == current.controller && last.holder == current.holder && last.container == current.container && (last.confirmed || last.renewal != current.renewal)
		}
		r.leaders.Store(deployment.UID, current)
		return current.confirmed, nil
	}
	return false, nil
}
