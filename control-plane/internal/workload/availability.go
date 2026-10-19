// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Observes Kubernetes workload availability and accelerator-node placement.

package workload

import (
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
)

// DeploymentAvailable checks that the Deployment reports all requested replicas available at its current generation.
func DeploymentAvailable(deployment *appsv1.Deployment) bool {
	if deployment == nil || deployment.Spec.Replicas == nil || *deployment.Spec.Replicas == 0 {
		return false
	}
	conditionAvailable := false
	for _, condition := range deployment.Status.Conditions {
		if condition.Type == appsv1.DeploymentAvailable && condition.Status == corev1.ConditionTrue {
			conditionAvailable = true
			break
		}
	}
	return deployment.Status.ObservedGeneration >= deployment.Generation &&
		deployment.Status.AvailableReplicas == *deployment.Spec.Replicas && conditionAvailable
}

// PodReady reads the Kubernetes readiness condition used by workload observers.
func PodReady(pod *corev1.Pod) bool {
	for _, condition := range pod.Status.Conditions {
		if condition.Type == corev1.PodReady {
			return condition.Status == corev1.ConditionTrue
		}
	}
	return false
}

// AcceleratorTolerations applies the same accelerator-node admission to both
// serving Pods and CPU preparation Jobs, without allocating a device to the Job.
func AcceleratorTolerations(resourceName string) []corev1.Toleration {
	return []corev1.Toleration{{Key: resourceName, Operator: corev1.TolerationOpExists, Effect: corev1.TaintEffectNoSchedule}}
}
