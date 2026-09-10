// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects platform-owned diagnostic storage without accepting per-capture Pod mutations.
package controllers

import (
	"fmt"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
)

// configureProfilingWorkload binds a prepared namespace to its dedicated artifact PVC.
// The ordinary ModelGroup controller owns deployment changes; ProfileRun never calls this path.
func configureProfilingWorkload(deployment *appsv1.Deployment, claim string) error {
	if claim == "" {
		return nil
	}
	pod := &deployment.Spec.Template.Spec
	for _, volume := range pod.Volumes {
		if volume.PersistentVolumeClaim != nil && volume.PersistentVolumeClaim.ClaimName == claim {
			return fmt.Errorf("profiling requires a dedicated PVC, separate from runtime cache and KV storage")
		}
	}
	pod.Volumes = append(pod.Volumes, corev1.Volume{Name: "profiling", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: claim}}})
	container := &pod.Containers[0]
	container.VolumeMounts = append(container.VolumeMounts, corev1.VolumeMount{Name: "profiling", MountPath: "/var/lib/foretoken/profiles"})
	container.Env = append(container.Env, corev1.EnvVar{Name: "FORETOKEN_PROFILE_ARTIFACT_CLAIM", Value: claim})
	for _, env := range container.Env {
		if env.Name == "FORETOKEN_POD_UID" {
			return nil
		}
	}
	container.Env = append(container.Env, corev1.EnvVar{Name: "FORETOKEN_POD_UID", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.uid"}}})
	return nil
}
