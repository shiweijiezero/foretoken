// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Owns model source acquisition, placement, and retained preparation records.

package model

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"slices"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/vllm"
	vllmomniconfig "github.com/shiweijiezero/foretoken/control-plane/internal/vllmomni"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// reconcilePreparation runs source acquisition before allocating ModelGroup accelerator Pods.
func (reconciler *GroupReconciler) reconcilePreparation(ctx context.Context, group *inferencev1alpha1.ModelGroup, pool *inferencev1alpha1.ModelPool) (bool, error) {
	// Existing cohorts retain their acquisition contract until the Pool replaces them.
	// Once materialized, the serving runtime owns cache recovery.
	if group.Spec.Runtime.PreparationVersion == 0 || meta.IsStatusConditionTrue(group.Status.Conditions, conditionWorkloadMaterialized) {
		return true, nil
	}
	if group.Spec.Artifacts.Source == inferencev1alpha1.ModelSourceLocal {
		return true, nil
	}
	if err := reconciler.reconcilePreparationSource(ctx, group, pool); err != nil {
		return false, err
	}
	desired, err := desiredPreparationJob(group, reconciler.ImagePullSecrets, reconciler.ModelDistribution, reconciler.ApplicationFiles)
	if err != nil {
		return false, err
	}
	if err := controllerutil.SetControllerReference(group, desired, reconciler.Scheme()); err != nil {
		return false, fmt.Errorf("set model preparation Job owner: %w", err)
	}
	current := new(batchv1.Job)
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(desired), current); apierrors.IsNotFound(err) {
		if group.Spec.Artifacts.Cache != nil {
			affinity, err := reconciler.preparationNodeAffinity(ctx, group)
			if err != nil {
				return false, err
			}
			desired.Spec.Template.Spec.Affinity = &corev1.Affinity{NodeAffinity: affinity}
		}
		if err := runtimeconfig.PlaceRuntimeCache(ctx, reconciler.Client, group.Namespace, group.Spec.Artifacts.Cache, &desired.Spec.Template); err != nil {
			return false, err
		}
		if err := reconciler.Create(ctx, desired); err != nil {
			return false, fmt.Errorf("create model preparation Job: %w", err)
		}
		return false, nil
	} else if err != nil {
		return false, fmt.Errorf("get model preparation Job: %w", err)
	}
	if !metav1.IsControlledBy(current, group) {
		return false, fmt.Errorf("model preparation Job %q is not controlled by ModelGroup", current.Name)
	}
	for _, condition := range current.Status.Conditions {
		if condition.Type == batchv1.JobFailed && condition.Status == corev1.ConditionTrue {
			// Preserve the failed attempt before retiring its diagnostics. Recovery
			// continues, but cannot turn an observed failure back into unknown readiness.
			failure := reconciler.preparationFailure(ctx, current, condition.Reason, condition.Message)
			if err := reconciler.updateStatus(ctx, group, modelGroupPreparationFailureState(failure)); err != nil {
				return false, errors.Join(failure, err)
			}
			if current.DeletionTimestamp.IsZero() {
				if err := reconciler.Delete(ctx, current, client.PropagationPolicy(metav1.DeletePropagationForeground), client.Preconditions{UID: &current.UID}); client.IgnoreNotFound(err) != nil {
					return false, errors.Join(failure, fmt.Errorf("retire failed model preparation Job: %w", err))
				}
			}
			return false, failure
		}
		if condition.Type == batchv1.JobComplete && condition.Status == corev1.ConditionTrue {
			return true, nil
		}
	}
	// Native Job backoff continues recovery without hiding an already failed attempt.
	if current.Status.Failed > 0 && !meta.IsStatusConditionFalse(group.Status.Conditions, workload.ConditionTargetReady) {
		failure := reconciler.preparationFailure(ctx, current, "ContainerFailed", "A preparation container exited unsuccessfully")
		return false, errors.Join(failure, reconciler.updateStatus(ctx, group, modelGroupPreparationFailureState(failure)))
	}
	return false, nil
}

// modelPreparationFailure distinguishes a completed failed attempt from an API observation error.
type modelPreparationFailure struct {
	reason  string
	message string
}

// Error retains the preparation cause for status publication and reconciliation logs.
func (failure *modelPreparationFailure) Error() string { return failure.message }

// preparationFailure captures the latest attempt's container diagnostics before Job retirement.
func (reconciler *GroupReconciler) preparationFailure(ctx context.Context, job *batchv1.Job, reason, message string) error {
	failure := &modelPreparationFailure{
		reason:  "ModelPreparationFailed",
		message: fmt.Sprintf("Model preparation Job %q failed: %s: %s", job.Name, reason, message),
	}
	if reason == "DeadlineExceeded" {
		failure.reason = "ModelPreparationDeadlineExceeded"
		if job.Spec.ActiveDeadlineSeconds != nil {
			failure.message = fmt.Sprintf("Model preparation Job %q exceeded its %s budget from spec.timeouts.startup", job.Name, time.Duration(*job.Spec.ActiveDeadlineSeconds)*time.Second)
		}
	}
	var pods corev1.PodList
	if err := reconciler.List(ctx, &pods, client.InNamespace(job.Namespace), client.MatchingLabels{"batch.kubernetes.io/controller-uid": string(job.UID)}); err != nil {
		return errors.Join(failure, fmt.Errorf("read preparation diagnostics: %w", err))
	}
	var latest *corev1.Pod
	for index := range pods.Items {
		pod := &pods.Items[index]
		if latest != nil {
			failed, previousFailed := pod.Status.Phase == corev1.PodFailed, latest.Status.Phase == corev1.PodFailed
			if previousFailed && !failed || failed == previousFailed && !pod.CreationTimestamp.After(latest.CreationTimestamp.Time) {
				continue
			}
		}
		latest = pod
	}
	if latest != nil {
		if latest.Status.Message != "" {
			failure.message += fmt.Sprintf("\n%s: %s: %s", latest.Name, latest.Status.Reason, latest.Status.Message)
		}
		for _, status := range append(slices.Clone(latest.Status.InitContainerStatuses), latest.Status.ContainerStatuses...) {
			if ended := status.State.Terminated; ended != nil && ended.ExitCode != 0 {
				failure.message += fmt.Sprintf("\n%s/%s: %s (exit %d): %s", latest.Name, status.Name, ended.Reason, ended.ExitCode, ended.Message)
			} else if waiting := status.State.Waiting; waiting != nil {
				failure.message += fmt.Sprintf("\n%s/%s: %s: %s", latest.Name, status.Name, waiting.Reason, waiting.Message)
			}
		}
	}
	failure.message += "\nKubernetes recovery continues."
	return failure
}

// desiredPreparationJob builds a CPU/network/storage-only source preparation workload.
func desiredPreparationJob(group *inferencev1alpha1.ModelGroup, imagePullSecrets []corev1.LocalObjectReference, distribution runtimeconfig.ModelDistributionProfile, files runtimeconfig.ApplicationFiles) (*batchv1.Job, error) {
	inputEnv := runtimeconfig.ModelArtifactsEnv
	var input any = runtimeconfig.ProjectModelArtifacts(group.Spec.Artifacts)
	if group.Spec.Runtime.PreparationVersion == 1 {
		plan, err := vllmconfig.BuildLaunchPlan(group.Spec)
		if err != nil {
			return nil, fmt.Errorf("build retained model preparation plan: %w", err)
		}
		input, inputEnv = plan, "FORETOKEN_VLLM_LAUNCH_PLAN"
	}
	inputJSON, err := json.Marshal(input)
	if err != nil {
		return nil, fmt.Errorf("marshal model preparation input: %w", err)
	}
	binary := "foretoken-model-server"
	if group.Spec.Runtime.Backend == vllmomniconfig.Backend {
		binary = "foretoken-omni-model-server"
	}
	cache := group.Spec.Artifacts.Cache
	if group.Spec.Artifacts.Source == inferencev1alpha1.ModelSourceLocal {
		return nil, fmt.Errorf("model preparation requires a remote source")
	}
	name := "prepare-" + string(group.UID)
	env := []corev1.EnvVar{
		{Name: inputEnv, Value: string(inputJSON)},
		{Name: "FORETOKEN_POD_UID", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.uid"}}},
		{Name: runtimeconfig.ModelPreparationScopeEnv, Value: group.Spec.ModelPoolRef.UID + "/" + group.Spec.Revision},
		{Name: runtimeconfig.PreparationConfigMapEnv, Value: runtimeconfig.PreparationSourceName(group.Spec)},
		{Name: preparationNamespaceEnv, Value: group.Namespace},
		// CUDA base images expose all devices unless CPU-only preparation overrides them.
		{Name: "NVIDIA_VISIBLE_DEVICES", Value: "void"},
	}
	env = append(env, vllmconfig.RuntimeCacheEnv(cache, group.Namespace, group.Labels[inferencev1alpha1.DeploymentUIDLabel], "", group.Spec.Runtime.PreparationVersion)...)
	env = append(env, runtimeconfig.HuggingFaceEnv(group.Spec.Artifacts.HuggingFaceAccess)...)
	startupSeconds, err := runtimeconfig.WholeDurationSeconds(group.Spec.Timeouts.Startup)
	if err != nil {
		return nil, fmt.Errorf("parse model preparation timeout: %w", err)
	}
	volumes := []corev1.Volume{{Name: "tmp", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}}
	mounts := []corev1.VolumeMount{{Name: "tmp", MountPath: "/tmp"}}
	command := "resolve"
	var nodeSelector map[string]string
	var tolerations []corev1.Toleration
	var ports []corev1.ContainerPort
	if cache != nil {
		command = "prepare"
		env = append(env, runtimeconfig.CacheObserverEnv(*cache, group.Spec.Runtime.Port)...)
		ports = append(ports, corev1.ContainerPort{Name: "cache-observe", ContainerPort: runtimeconfig.CacheObservationPort(group.Spec.Runtime.Port), Protocol: corev1.ProtocolTCP})
		volumes = append(volumes, corev1.Volume{Name: runtimeconfig.CacheVolumeName, VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: cache.ClaimName}}})
		mounts = append(mounts, corev1.VolumeMount{Name: runtimeconfig.CacheVolumeName, MountPath: cache.MountPath})
		nodeSelector = maps.Clone(group.Spec.Accelerator.NodeSelector)
		tolerations = workload.AcceleratorTolerations(group.Spec.Accelerator.DeviceResourceName)
	}
	if cache != nil && distribution.ProxyURL != "" {
		env = append(env, corev1.EnvVar{Name: runtimeconfig.ModelDownloadProxyEnv, Value: distribution.ProxyURL})
	}
	if groupApplicationURL(group, files) != "" {
		env = append(env, corev1.EnvVar{Name: runtimeconfig.SourceDirectoryEnv, Value: files.Directory()})
	}
	automountToken := true
	job := &batchv1.Job{
		TypeMeta:   metav1.TypeMeta{APIVersion: batchv1.SchemeGroupVersion.String(), Kind: "Job"},
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: group.Namespace, Labels: workload.ModelGroupLabels(group)},
		Spec: batchv1.JobSpec{
			ActiveDeadlineSeconds: &startupSeconds,
			Template: corev1.PodTemplateSpec{
				// Preparation Pods must not match serving member or cache-observer selectors.
				ObjectMeta: metav1.ObjectMeta{Labels: map[string]string{workload.ModelPreparationGroupLabel: group.Name}},
				Spec: corev1.PodSpec{
					ServiceAccountName:           runtimeconfig.PreparationSourceName(group.Spec),
					AutomountServiceAccountToken: &automountToken,
					RestartPolicy:                corev1.RestartPolicyNever,
					ImagePullSecrets:             slices.Clone(imagePullSecrets),
					NodeSelector:                 nodeSelector,
					Tolerations:                  tolerations,
					Volumes:                      volumes,
					Containers: []corev1.Container{{
						Name:                     "model-preparation",
						Image:                    group.Spec.Runtime.Image,
						ImagePullPolicy:          corev1.PullIfNotPresent,
						Args:                     []string{command},
						Ports:                    ports,
						Env:                      env,
						VolumeMounts:             mounts,
						TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
					}},
				},
			},
		},
	}
	if revision := group.Spec.Runtime.SourceRevision; revision != "" {
		job.Spec.Template.Annotations = map[string]string{runtimeconfig.SourceRevisionAnnotation: revision}
	}
	files.Configure(&job.Spec.Template, &job.Spec.Template.Spec.Containers[0], groupApplicationURL(group, files), binary)
	access := runtimeconfig.WorkloadCacheAccess(&job.Spec.Template, cache, group.Spec.Runtime.PreparationVersion)
	runtimeconfig.ConfigurePodStorage(&job.Spec.Template.Spec, files.Image, access.Identity, access)
	return job, nil
}

var errNoPreparationPlacement = errors.New("no eligible accelerator node for model preparation")

// preparationNodeAffinity restricts a new CPU preparation Job to nodes that can
// also host its serving Pod. The scheduler still decides placement and PVC binding.
func (reconciler *GroupReconciler) preparationNodeAffinity(ctx context.Context, group *inferencev1alpha1.ModelGroup) (*corev1.NodeAffinity, error) {
	var nodes corev1.NodeList
	if err := reconciler.List(ctx, &nodes, client.MatchingLabels(group.Spec.Accelerator.NodeSelector)); err != nil {
		return nil, fmt.Errorf("list accelerator nodes for model preparation: %w", err)
	}
	resourceName := corev1.ResourceName(group.Spec.Accelerator.DeviceResourceName)
	var terms []corev1.NodeSelectorTerm
	for _, node := range nodes.Items {
		allocatable := node.Status.Allocatable[resourceName]
		if node.Spec.Unschedulable || allocatable.Value() < int64(group.Spec.Resources.Requests.GPU.Count) {
			continue
		}
		ready := false
		for _, condition := range node.Status.Conditions {
			if condition.Type == corev1.NodeReady && condition.Status == corev1.ConditionTrue {
				ready = true
				break
			}
		}
		if !ready || !preparationNodeTaintsSupported(node.Spec.Taints, string(resourceName)) {
			continue
		}
		// Node field selectors accept one value; separate terms express eligible alternatives.
		terms = append(terms, corev1.NodeSelectorTerm{MatchFields: []corev1.NodeSelectorRequirement{{
			Key: "metadata.name", Operator: corev1.NodeSelectorOpIn, Values: []string{node.Name},
		}}})
	}
	if len(terms) == 0 {
		return nil, errNoPreparationPlacement
	}
	return &corev1.NodeAffinity{RequiredDuringSchedulingIgnoredDuringExecution: &corev1.NodeSelector{
		NodeSelectorTerms: terms,
	}}, nil
}

// preparationNodeTaintsSupported excludes nodes neither Pod can use.
func preparationNodeTaintsSupported(taints []corev1.Taint, accelerator string) bool {
	for _, taint := range taints {
		if taint.Effect != corev1.TaintEffectNoSchedule && taint.Effect != corev1.TaintEffectNoExecute {
			continue
		}
		if taint.Key != accelerator || taint.Effect != corev1.TaintEffectNoSchedule {
			return false
		}
	}
	return true
}
