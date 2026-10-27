// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Owns model preparation Jobs, placement, and failure reporting.

package model

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"math"
	"slices"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	vllmconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllm"
	vllmomniconfig "github.com/shiweijiezero/foretoken/control-plane/internal/engines/vllmomni"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	"github.com/shiweijiezero/foretoken/control-plane/internal/workload"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/component-helpers/scheduling/corev1/nodeaffinity"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// modelPreparationFailure distinguishes a completed failed attempt from an API observation error.
type modelPreparationFailure struct {
	reason  string
	message string
}

var errNoPreparationPlacement = errors.New("no eligible accelerator node for model preparation")

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
	startupSeconds, err := runtimeconfig.WholeDurationSeconds(group.Spec.Timeouts.Startup)
	if err != nil {
		return false, fmt.Errorf("parse model preparation timeout: %w", err)
	}
	desired, err := desiredPreparationJob(group, reconciler.ImagePullSecrets, reconciler.ModelDistribution, reconciler.ApplicationFiles)
	if err != nil {
		return false, err
	}
	preparation, err := reconciler.preparationJob(ctx, group, desired.Name)
	if err != nil {
		return false, err
	}
	runtime, err := reconciler.preparationJob(ctx, group, runtimePreparationName(group))
	if err != nil {
		return false, err
	}
	jobs := []*batchv1.Job{preparation, runtime}

	// Observe both owners before creating another attempt or admitting execution.
	// Foreground retirement keeps a cancelled sibling from writing into a new attempt.
	for _, job := range jobs {
		if job != nil && !job.DeletionTimestamp.IsZero() {
			return false, reconciler.retirePreparationJobs(ctx, jobs)
		}
	}
	completed := map[string]bool{}
	for _, job := range jobs {
		if job == nil {
			continue
		}
		for _, condition := range job.Status.Conditions {
			if condition.Status != corev1.ConditionTrue {
				continue
			}
			switch condition.Type {
			case batchv1.JobFailed, batchv1.JobFailureTarget:
				observedFailure := reconciler.preparationFailure(ctx, job, group.Spec.Timeouts.Startup, condition.Reason, condition.Message)
				failure := observedFailure
				if previous := meta.FindStatusCondition(group.Status.Conditions, workload.ConditionTargetReady); previous != nil && previous.ObservedGeneration == group.Generation && previous.Status == metav1.ConditionFalse && (previous.Reason == "ModelPreparationFailed" || previous.Reason == "ModelPreparationDeadlineExceeded") {
					failure = &modelPreparationFailure{reason: previous.Reason, message: previous.Message}
				}
				if err := reconciler.updateStatus(ctx, group, modelGroupPreparationFailureState(failure)); err != nil {
					return false, errors.Join(failure, err)
				}
				return false, errors.Join(observedFailure, reconciler.retirePreparationJobs(ctx, jobs))
			case batchv1.JobComplete:
				completed[job.Name] = true
			}
		}
		if !completed[job.Name] {
			if err := reconciler.startPreparationDeadline(ctx, job, startupSeconds); err != nil {
				return false, err
			}
			// Native Job backoff retains the budget across process retries.
			if job.Status.Failed > 0 && !meta.IsStatusConditionFalse(group.Status.Conditions, workload.ConditionTargetReady) {
				failure := reconciler.preparationFailure(ctx, job, group.Spec.Timeouts.Startup, "ContainerFailed", "A preparation container exited unsuccessfully")
				return false, errors.Join(failure, reconciler.updateStatus(ctx, group, modelGroupPreparationFailureState(failure)))
			}
		}
	}
	if preparation == nil {
		if runtime != nil {
			return false, reconciler.retirePreparationJobs(ctx, jobs)
		}
		image, err := reconciler.preparationEnvironment(ctx, group)
		if err != nil {
			return false, err
		}
		desired.Spec.Template.Spec.Containers[0].Image = image
		return false, reconciler.createPreparationJob(ctx, group, desired, "")
	}

	// Retain the actual environment of in-flight Jobs across platform upgrades.
	// An original engine-image Job already prepares both artifacts and runtime.
	if preparation.Spec.Template.Spec.Containers[0].Image == group.Spec.Runtime.Image {
		return completed[preparation.Name], nil
	}
	// Refresh an unstarted pair if its previously eligible nodes disappear.
	// Active processes retain their original native Job execution budget.
	for _, job := range jobs {
		if job == nil || completed[job.Name] || job.Spec.ActiveDeadlineSeconds != nil {
			continue
		}
		nodes, err := reconciler.preparationNodes(ctx, group)
		if err != nil && !errors.Is(err, errNoPreparationPlacement) {
			return false, err
		}
		required := nodeaffinity.GetRequiredNodeAffinity(&corev1.Pod{Spec: job.Spec.Template.Spec})
		available := false
		for index := range nodes {
			matches, err := required.Match(&nodes[index])
			if err != nil {
				return false, fmt.Errorf("evaluate model preparation placement: %w", err)
			}
			available = available || matches
		}
		if !available {
			return false, reconciler.retirePreparationJobs(ctx, jobs)
		}
	}
	if runtime == nil {
		var pods corev1.PodList
		if err := reconciler.List(ctx, &pods, client.InNamespace(group.Namespace), client.MatchingLabels{"batch.kubernetes.io/controller-uid": string(preparation.UID)}); err != nil {
			return false, fmt.Errorf("observe model preparation placement: %w", err)
		}
		var latest *corev1.Pod
		for index := range pods.Items {
			pod := &pods.Items[index]
			started := firstContainerStart([]corev1.Pod{*pod}, preparation.Spec.Template.Spec.Containers[0].Name)
			if pod.Spec.NodeName != "" && !started.IsZero() && pod.DeletionTimestamp.IsZero() && pod.Status.Phase != corev1.PodFailed && (latest == nil || pod.CreationTimestamp.After(latest.CreationTimestamp.Time)) {
				latest = pod
			}
		}
		if latest == nil && !completed[preparation.Name] {
			return false, nil
		}
		preferredNode := ""
		if latest != nil {
			preferredNode = latest.Spec.NodeName
		}
		desired.Name = runtimePreparationName(group)
		return false, reconciler.createPreparationJob(ctx, group, desired, preferredNode)
	}
	return completed[preparation.Name] && completed[runtime.Name], nil
}

// preparationEnvironment separates CPU acquisition when both preparation workers
// can share storage and the serving workload has a unique eligible destination.
func (reconciler *GroupReconciler) preparationEnvironment(ctx context.Context, group *inferencev1alpha1.ModelGroup) (string, error) {
	cache := group.Spec.Artifacts.Cache
	if reconciler.PreparationImage == "" || cache == nil || group.Spec.NodeCount != 1 || group.Spec.Runtime.PreparationVersion < runtimeconfig.SharedModelCachePreparationVersion || groupApplicationURL(group, reconciler.ApplicationFiles) == "" {
		return group.Spec.Runtime.Image, nil
	}
	claim, err := runtimeconfig.ReadPreparedCachePVC(ctx, reconciler.Client, group.Namespace, cache.ClaimName)
	if err != nil {
		return "", err
	}
	if !slices.Contains(claim.Spec.AccessModes, corev1.ReadWriteMany) {
		return group.Spec.Runtime.Image, nil
	}
	nodes, err := reconciler.preparationNodes(ctx, group)
	if err != nil {
		return "", err
	}
	if len(nodes) != 1 {
		return group.Spec.Runtime.Image, nil
	}
	// A cached engine needs no second preparation Job. Node status is only a hint:
	// the ordinary Job still pulls the image if it has since been collected.
	for _, image := range nodes[0].Status.Images {
		if slices.Contains(image.Names, group.Spec.Runtime.Image) {
			return group.Spec.Runtime.Image, nil
		}
	}
	return reconciler.PreparationImage, nil
}

// preparationJob reads a preparation owner without adopting a same-name replacement.
func (reconciler *GroupReconciler) preparationJob(ctx context.Context, group *inferencev1alpha1.ModelGroup, name string) (*batchv1.Job, error) {
	job := new(batchv1.Job)
	if err := reconciler.Get(ctx, client.ObjectKey{Namespace: group.Namespace, Name: name}, job); apierrors.IsNotFound(err) {
		return nil, nil
	} else if err != nil {
		return nil, fmt.Errorf("read model preparation Job: %w", err)
	}
	if !metav1.IsControlledBy(job, group) {
		return nil, fmt.Errorf("model preparation Job %q is not controlled by ModelGroup", name)
	}
	return job, nil
}

// createPreparationJob preserves native scheduling and storage placement for both preparation stages.
func (reconciler *GroupReconciler) createPreparationJob(ctx context.Context, group *inferencev1alpha1.ModelGroup, job *batchv1.Job, preferredNode string) error {
	if err := controllerutil.SetControllerReference(group, job, reconciler.Scheme()); err != nil {
		return fmt.Errorf("set model preparation Job owner: %w", err)
	}
	if group.Spec.Artifacts.Cache != nil {
		affinity, err := reconciler.preparationNodeAffinity(ctx, group)
		if err != nil {
			return err
		}
		// Prefer the downloader's node for image reuse, without pinning recovery
		// to a node that may disappear between preparation attempts.
		if preferredNode != "" {
			affinity.PreferredDuringSchedulingIgnoredDuringExecution = append(affinity.PreferredDuringSchedulingIgnoredDuringExecution, corev1.PreferredSchedulingTerm{
				Weight:     100,
				Preference: corev1.NodeSelectorTerm{MatchFields: []corev1.NodeSelectorRequirement{{Key: "metadata.name", Operator: corev1.NodeSelectorOpIn, Values: []string{preferredNode}}}},
			})
		}
		job.Spec.Template.Spec.Affinity = &corev1.Affinity{NodeAffinity: affinity}
	}
	if err := runtimeconfig.PlaceRuntimeCache(ctx, reconciler.Client, group.Namespace, group.Spec.Artifacts.Cache, &job.Spec.Template); err != nil {
		return err
	}
	if err := reconciler.Create(ctx, job); err != nil {
		return fmt.Errorf("create model preparation Job: %w", err)
	}
	return nil
}

// retirePreparationJobs joins both native Job lifetimes before a replacement attempt.
func (reconciler *GroupReconciler) retirePreparationJobs(ctx context.Context, jobs []*batchv1.Job) error {
	var failures []error
	for _, job := range jobs {
		if job != nil && job.DeletionTimestamp.IsZero() {
			if err := reconciler.Delete(ctx, job, client.PropagationPolicy(metav1.DeletePropagationForeground), client.Preconditions{UID: &job.UID}); client.IgnoreNotFound(err) != nil {
				failures = append(failures, fmt.Errorf("retire model preparation Job: %w", err))
			}
		}
	}
	return errors.Join(failures...)
}

func runtimePreparationName(group *inferencev1alpha1.ModelGroup) string {
	return "prepare-runtime-" + string(group.UID)
}

// startPreparationDeadline gives the first preparation process its budget; the native Job owns enforcement across retries.
func (reconciler *GroupReconciler) startPreparationDeadline(ctx context.Context, job *batchv1.Job, startupSeconds int64) error {
	if job.Spec.ActiveDeadlineSeconds != nil || job.Status.StartTime == nil {
		return nil
	}
	var pods corev1.PodList
	if err := reconciler.List(ctx, &pods, client.InNamespace(job.Namespace), client.MatchingLabels{"batch.kubernetes.io/controller-uid": string(job.UID)}); err != nil {
		return fmt.Errorf("observe model preparation startup: %w", err)
	}
	started := firstContainerStart(pods.Items, job.Spec.Template.Spec.Containers[0].Name)
	if started.IsZero() {
		return nil
	}
	// Job deadlines are relative to Job startTime, which includes image pulls and scheduling.
	deadline := started.Add(time.Duration(startupSeconds) * time.Second)
	seconds := max(int64(1), int64(math.Ceil(deadline.Sub(job.Status.StartTime.Time).Seconds())))
	base := job.DeepCopy()
	job.Spec.ActiveDeadlineSeconds = &seconds
	if err := reconciler.Patch(ctx, job, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		return fmt.Errorf("start model preparation deadline: %w", err)
	}
	return nil
}

// Error retains the preparation cause for status publication and reconciliation logs.
func (failure *modelPreparationFailure) Error() string { return failure.message }

// preparationFailure captures the latest attempt's container diagnostics before Job retirement.
func (reconciler *GroupReconciler) preparationFailure(ctx context.Context, job *batchv1.Job, startup inferencev1alpha1.Duration, reason, message string) error {
	failure := &modelPreparationFailure{
		reason:  "ModelPreparationFailed",
		message: fmt.Sprintf("Model preparation Job %q failed: %s: %s", job.Name, reason, message),
	}
	if reason == "DeadlineExceeded" {
		failure.reason = "ModelPreparationDeadlineExceeded"
		failure.message = fmt.Sprintf("Model preparation Job %q exceeded its %s budget from spec.timeouts.startup", job.Name, startup)
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

// preparationNodeAffinity restricts a new CPU preparation Job to nodes that can
// also host its serving Pod. The scheduler still decides placement and PVC binding.
func (reconciler *GroupReconciler) preparationNodeAffinity(ctx context.Context, group *inferencev1alpha1.ModelGroup) (*corev1.NodeAffinity, error) {
	nodes, err := reconciler.preparationNodes(ctx, group)
	if err != nil {
		return nil, err
	}
	var terms []corev1.NodeSelectorTerm
	for _, node := range nodes {
		// Node field selectors accept one value; separate terms express eligible alternatives.
		terms = append(terms, corev1.NodeSelectorTerm{MatchFields: []corev1.NodeSelectorRequirement{{
			Key: "metadata.name", Operator: corev1.NodeSelectorOpIn, Values: []string{node.Name},
		}}})
	}
	return &corev1.NodeAffinity{RequiredDuringSchedulingIgnoredDuringExecution: &corev1.NodeSelector{
		NodeSelectorTerms: terms,
	}}, nil
}

// preparationNodes shares serving-capacity selection between initial placement and
// recovery when an unstarted runtime preparation no longer has an eligible node.
func (reconciler *GroupReconciler) preparationNodes(ctx context.Context, group *inferencev1alpha1.ModelGroup) ([]corev1.Node, error) {
	var nodes corev1.NodeList
	if err := reconciler.List(ctx, &nodes, client.MatchingLabels(group.Spec.Accelerator.NodeSelector)); err != nil {
		return nil, fmt.Errorf("list accelerator nodes for model preparation: %w", err)
	}
	requests := modelResourceRequirements(group.Spec.Resources, group.Spec.Accelerator.DeviceResourceName, group.Spec.RDMA).Requests
	var placement *nodeaffinity.NodeSelector
	if rdma := group.Spec.RDMA; rdma != nil && rdma.RequiredNodeAffinity != nil {
		var err error
		placement, err = nodeaffinity.NewNodeSelector(rdma.RequiredNodeAffinity)
		if err != nil {
			return nil, fmt.Errorf("resolve model preparation placement: %w", err)
		}
	}
	nodes.Items = slices.DeleteFunc(nodes.Items, func(node corev1.Node) bool {
		return !modelNodeFitsRequests(node, group.Spec.Accelerator.DeviceResourceName, requests, placement)
	})
	if len(nodes.Items) == 0 {
		return nil, errNoPreparationPlacement
	}
	return nodes.Items, nil
}
