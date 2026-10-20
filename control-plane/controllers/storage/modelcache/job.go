// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Runs CPU-only model-cache collection workers with platform-local claims.

package modelcache

import (
	"encoding/json"
	"fmt"
	"slices"

	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// collectionJob reuses platform applications and storage identity without changing shared-root permissions.
func (reconciler *Reconciler) collectionJob(plan modelCacheCollectionPlan) (*batchv1.Job, error) {
	payload, err := json.Marshal(plan)
	if err != nil {
		return nil, fmt.Errorf("encode model cache collection plan: %w", err)
	}
	automount, noEscalation, readOnly := true, false, true
	job := &batchv1.Job{
		ObjectMeta: reconciler.collectionMetadata(plan.Record, modelCacheCollectionJobPrefix+string(plan.Record.UID), reconciler.Namespace),
		Spec: batchv1.JobSpec{Template: corev1.PodTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{CollectionLabel: string(plan.Record.UID)}},
			Spec: corev1.PodSpec{
				ServiceAccountName: reconciler.ServiceAccount, AutomountServiceAccountToken: &automount,
				RestartPolicy: corev1.RestartPolicyNever, ImagePullSecrets: slices.Clone(reconciler.ImagePullSecrets),
				Volumes: []corev1.Volume{
					{Name: runtimeconfig.CacheVolumeName, VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: plan.CollectionVolume.ClaimName}}},
					{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
				},
				Containers: []corev1.Container{{
					Name: "model-cache-collection", Image: reconciler.WorkerImage, ImagePullPolicy: corev1.PullIfNotPresent,
					Command: []string{"foretoken-model-server"}, Args: []string{"collect-cache"},
					Env: []corev1.EnvVar{
						{Name: modelCacheCollectionEnv, Value: string(payload)},
						{Name: runtimeconfig.ModelCacheRootEnv, Value: runtimeconfig.ModelCacheDirectory(plan.Cache.MountPath)},
						{Name: "NVIDIA_VISIBLE_DEVICES", Value: "void"},
					},
					VolumeMounts: []corev1.VolumeMount{
						{Name: runtimeconfig.CacheVolumeName, MountPath: plan.Cache.MountPath},
						{Name: "tmp", MountPath: "/tmp"},
					},
					SecurityContext: &corev1.SecurityContext{
						AllowPrivilegeEscalation: &noEscalation, ReadOnlyRootFilesystem: &readOnly,
						Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
					},
					TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
				}},
			},
		}},
	}
	if reconciler.DeviceResourceName != "" {
		job.Spec.Template.Spec.Tolerations = workload.AcceleratorTolerations(reconciler.DeviceResourceName)
	}
	if reconciler.WorkerApplicationURL != "" {
		job.Spec.Template.Spec.Containers[0].Env = append(job.Spec.Template.Spec.Containers[0].Env, corev1.EnvVar{Name: runtimeconfig.SourceDirectoryEnv, Value: reconciler.ApplicationFiles.Directory()})
	}
	reconciler.ApplicationFiles.Configure(&job.Spec.Template, &job.Spec.Template.Spec.Containers[0], reconciler.WorkerApplicationURL, "foretoken-model-server")
	identity := runtimeconfig.DefaultStorageIdentity()
	if plan.Cache.DirectoryOwner != nil {
		identity = *plan.Cache.DirectoryOwner
	}
	runtimeconfig.ConfigurePodStorage(&job.Spec.Template.Spec, reconciler.ApplicationFiles.Image, identity,
		runtimeconfig.VolumeAccess{Name: runtimeconfig.CacheVolumeName, Identity: identity})
	return job, nil
}
