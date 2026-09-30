// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles durable VideoTask intent, execution, retention, and stored-file cleanup.
package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"regexp"
	"time"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const (
	videoTaskFinalizer    = "inference.foretoken.io/video-task-cleanup"
	videoTaskContract     = "v1"
	videoTaskOutputMount  = "/var/lib/foretoken/video-tasks"
	videoFrontendUIDLabel = "inference.foretoken.io/video-frontend-uid"
)

var videoTaskNamePattern = regexp.MustCompile(`^video-[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$`)

// VideoTaskReconciler owns worker Jobs, retention and PVC cleanup for VideoTasks.
type VideoTaskReconciler struct {
	client.Client
	APIReader        client.Reader
	WorkerImage      string
	FrontendPort     int32
	ImagePullSecrets []corev1.LocalObjectReference
}

// SetupWithManager registers task and owned-Job watches for lifecycle reconciliation.
func (r *VideoTaskReconciler) SetupWithManager(manager ctrl.Manager) error {
	r.APIReader = manager.GetAPIReader()
	return ctrl.NewControllerManagedBy(manager).
		For(&api.VideoTask{}).
		Owns(&batchv1.Job{}).
		Complete(r)
}

// Reconcile persists execution identity before creating a Job and retains terminal state until expiration.
func (r *VideoTaskReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	task := new(api.VideoTask)
	if err := r.APIReader.Get(ctx, request.NamespacedName, task); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !controllerutil.ContainsFinalizer(task, videoTaskFinalizer) && task.DeletionTimestamp.IsZero() {
		base := task.DeepCopy()
		controllerutil.AddFinalizer(task, videoTaskFinalizer)
		return ctrl.Result{Requeue: true}, r.Patch(ctx, task, client.MergeFrom(base))
	}
	if !task.DeletionTimestamp.IsZero() || task.Status.ExpiresAt != nil && !time.Now().Before(task.Status.ExpiresAt.Time) {
		return r.cleanupVideoTask(ctx, task)
	}
	if videoTaskTerminal(task.Status.Phase) {
		if task.Status.ExpiresAt == nil {
			return r.finishVideoTask(ctx, task)
		}
		return ctrl.Result{RequeueAfter: time.Until(task.Status.ExpiresAt.Time)}, nil
	}
	if task.Status.Plan == nil {
		plan, err := r.prepareVideoTask(ctx, task)
		if plan.OutputClaimName != "" {
			task.Status.Plan = &plan
		}
		if err != nil && (!task.Spec.CancelRequested || task.Status.Plan == nil) {
			task.Status.Phase, task.Status.Reason, task.Status.Message = "Failed", "TargetUnavailable", err.Error()
			return r.finishVideoTask(ctx, task)
		}
		task.Status.Phase = "Pending"
		return ctrl.Result{Requeue: true}, r.writeVideoTaskStatus(ctx, task)
	}
	if task.Spec.CancelRequested {
		if done, err := r.stopVideoWorker(ctx, task); err != nil || !done {
			return ctrl.Result{RequeueAfter: 2 * time.Second}, err
		}
		task.Status.Phase, task.Status.Reason, task.Status.Message = "Cancelled", "CancellationRequested", "Worker Job was stopped"
		return r.finishVideoTask(ctx, task)
	}
	if !task.Spec.InputsReady && task.Status.JobUID == "" {
		deadline := task.CreationTimestamp.Add(time.Duration(task.Status.Plan.TimeoutSeconds) * time.Second)
		if !time.Now().Before(deadline) {
			task.Status.Phase, task.Status.Reason, task.Status.Message = "Failed", "UploadIncomplete", "task input files were not finalized before upload timeout"
			return r.finishVideoTask(ctx, task)
		}
		if task.Status.Phase != "Pending" {
			task.Status.Phase = "Pending"
			if err := r.writeVideoTaskStatus(ctx, task); err != nil {
				return ctrl.Result{}, err
			}
		}
		return ctrl.Result{RequeueAfter: min(time.Until(deadline), 30*time.Second)}, nil
	}

	plan := *task.Status.Plan
	job := new(batchv1.Job)
	err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: task.Namespace, Name: plan.JobName}, job)
	if err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	if apierrors.IsNotFound(err) {
		// A Job observed before and subsequently removed must never start a second generation.
		if task.Status.JobCreationRequested || task.Status.JobUID != "" {
			task.Status.Phase, task.Status.Reason, task.Status.Message = "Failed", "JobMissing", "worker Job disappeared or creation was interrupted; generation will not be retried"
			return r.finishVideoTask(ctx, task)
		}
		service := new(api.ModelService)
		if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: task.Namespace, Name: task.Spec.ModelServiceRef.Name}, service); err != nil {
			return ctrl.Result{}, err
		}
		if service.Status.ServingGeneration != plan.ServingGeneration || string(service.UID) != plan.ServiceUID || !reflect.DeepEqual(service.Status.ServingPoolRevisions, plan.Revisions) {
			task.Status.Phase, task.Status.Reason, task.Status.Message = "Failed", "TargetChanged", "ModelService serving identity changed before worker creation"
			return r.finishVideoTask(ctx, task)
		}
		if _, err := r.verifyVideoTaskFrontend(ctx, task); err != nil {
			task.Status.Phase, task.Status.Reason, task.Status.Message = "Failed", "FrontendChanged", err.Error()
			return r.finishVideoTask(ctx, task)
		}
		job, err = r.newVideoWorkerJob(task, plan, false)
		if err != nil {
			return ctrl.Result{}, err
		}
		// Persist the one-shot create decision before the API server can start a Pod.
		task.Status.JobCreationRequested = true
		task.Status.Phase = "Starting"
		if err := r.writeVideoTaskStatus(ctx, task); err != nil {
			return ctrl.Result{}, err
		}
		if err := r.Create(ctx, job); err != nil {
			if apierrors.IsAlreadyExists(err) {
				return ctrl.Result{Requeue: true}, nil
			}
			return ctrl.Result{}, err
		}
	}
	if !metav1.IsControlledBy(job, task) || task.Status.JobUID != "" && task.Status.JobUID != string(job.UID) {
		task.Status.Phase, task.Status.Reason, task.Status.Message = "Failed", "JobConflict", "worker Job ownership or UID does not match this VideoTask"
		return r.finishVideoTask(ctx, task)
	}
	task.Status.JobName, task.Status.JobUID = job.Name, string(job.UID)
	if task.Status.StartedAt == nil && job.Status.StartTime != nil {
		task.Status.StartedAt = job.Status.StartTime.DeepCopy()
	}
	for _, condition := range job.Status.Conditions {
		if condition.Status != corev1.ConditionTrue {
			continue
		}
		switch condition.Type {
		case batchv1.JobComplete:
			task.Status.Phase = "Succeeded"
			task.Status.Artifact = &api.VideoArtifactReference{ClaimName: plan.OutputClaimName, Path: plan.OutputPath}
			task.Status.Message = "worker Job completed and persisted its artifact"
			return r.finishVideoTask(ctx, task)
		case batchv1.JobFailed:
			task.Status.Phase, task.Status.Reason, task.Status.Message = "Failed", "WorkerFailed", condition.Message
			return r.finishVideoTask(ctx, task)
		}
	}
	task.Status.Phase = "Running"
	return ctrl.Result{}, r.writeVideoTaskStatus(ctx, task)
}

// verifyVideoTaskFrontend rejects worker authority not derived from the admitting FrontendService.
func (r *VideoTaskReconciler) verifyVideoTaskFrontend(ctx context.Context, task *api.VideoTask) (*api.FrontendService, error) {
	if !videoTaskNamePattern.MatchString(task.Name) {
		return nil, fmt.Errorf("VideoTask name must be a video-prefixed UUID")
	}
	var frontends api.FrontendServiceList
	if err := r.APIReader.List(ctx, &frontends, client.InNamespace(task.Namespace)); err != nil {
		return nil, err
	}
	for i := range frontends.Items {
		frontend := &frontends.Items[i]
		if string(frontend.UID) != task.Spec.FrontendUID || !frontend.DeletionTimestamp.IsZero() || frontend.Spec.VideoTasks == nil {
			continue
		}
		worker := task.Spec.Worker
		endpoint := fmt.Sprintf("http://%s.%s.svc:%d", frontend.Name, frontend.Namespace, r.FrontendPort)
		if task.Labels[videoFrontendUIDLabel] != task.Spec.FrontendUID || worker.Image != r.WorkerImage || worker.Endpoint != endpoint || worker.OutputClaimName != frontend.Spec.VideoTasks.ClaimName || worker.OutputPath != fmt.Sprintf("tasks/%s/result.mp4", task.Name) {
			return nil, fmt.Errorf("VideoTask worker does not match its FrontendService configuration")
		}
		return frontend, nil
	}
	return nil, fmt.Errorf("the admitting FrontendService no longer exists or video tasks are disabled")
}

// prepareVideoTask snapshots the ready ModelService before any worker executes.
func (r *VideoTaskReconciler) prepareVideoTask(ctx context.Context, task *api.VideoTask) (api.VideoExecutionPlan, error) {
	frontend, err := r.verifyVideoTaskFrontend(ctx, task)
	if err != nil {
		return api.VideoExecutionPlan{}, err
	}
	timeout, err := durationSeconds(frontend.Spec.Timeouts.Request)
	if err != nil {
		return api.VideoExecutionPlan{}, fmt.Errorf("parse frontend video request timeout: %w", err)
	}
	plan := api.VideoExecutionPlan{
		JobName: task.Name, OutputClaimName: frontend.Spec.VideoTasks.ClaimName,
		OutputPath:  fmt.Sprintf("tasks/%s/result.mp4", task.Name),
		WorkerImage: r.WorkerImage, RetentionSeconds: frontend.Spec.VideoTasks.RetentionSeconds, TimeoutSeconds: timeout,
	}
	service := new(api.ModelService)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: task.Namespace, Name: task.Spec.ModelServiceRef.Name}, service); err != nil {
		return plan, err
	}
	if service.Spec.Backend != "vllm-omni" {
		return plan, fmt.Errorf("video generation requires a vllm-omni ModelService")
	}
	if !modelServiceReady(service) || !meta.IsStatusConditionTrue(service.Status.Conditions, conditionReady) {
		return plan, fmt.Errorf("ModelService must already be Ready")
	}
	plan.Model, plan.ServiceUID, plan.ServingGeneration = service.Spec.Model, string(service.UID), service.Status.ServingGeneration
	plan.Revisions = append([]api.ServingPoolRevision(nil), service.Status.ServingPoolRevisions...)
	return plan, nil
}

// newVideoWorkerJob runs either inference or idempotent task-directory cleanup under the same storage identity.
func (r *VideoTaskReconciler) newVideoWorkerJob(task *api.VideoTask, plan api.VideoExecutionPlan, cleanup bool) (*batchv1.Job, error) {
	requestJSON, err := json.Marshal(task.Spec.Request)
	if err != nil {
		return nil, err
	}
	name := plan.JobName
	command := []string{"/video-worker"}
	env := []corev1.EnvVar{
		{Name: "FORETOKEN_VIDEO_TASK_ID", Value: task.Name},
		{Name: "FORETOKEN_VIDEO_OUTPUT_MOUNT", Value: videoTaskOutputMount},
	}
	if cleanup {
		name = task.Status.CleanupJobName
		command = append(command, "--cleanup")
	} else {
		env = append(env,
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_TASK_UID", Value: string(task.UID)},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_MODEL_SERVICE", Value: task.Spec.ModelServiceRef.Name},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_ENDPOINT", Value: task.Spec.Worker.Endpoint},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_MODEL", Value: plan.Model},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_REQUEST_JSON", Value: string(requestJSON)},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_OUTPUT_PATH", Value: plan.OutputPath},
			corev1.EnvVar{Name: "FORETOKEN_VIDEO_TIMEOUT_SECONDS", Value: fmt.Sprint(plan.TimeoutSeconds)},
		)
	}
	labels := map[string]string{"inference.foretoken.io/video-task": task.Name}
	noToken, nonRoot, noEscalation, readOnly := false, true, false, true
	group := int64(65532)
	job := &batchv1.Job{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: task.Namespace, Labels: labels, Annotations: map[string]string{
			"inference.foretoken.io/video-task-contract": videoTaskContract,
			"inference.foretoken.io/video-task-uid":      string(task.UID),
		}},
		Spec: batchv1.JobSpec{BackoffLimit: ptrInt32(0), ActiveDeadlineSeconds: &plan.TimeoutSeconds, Template: corev1.PodTemplateSpec{
			ObjectMeta: metav1.ObjectMeta{Labels: labels},
			Spec: corev1.PodSpec{
				RestartPolicy:                corev1.RestartPolicyNever,
				AutomountServiceAccountToken: &noToken,
				ImagePullSecrets:             append([]corev1.LocalObjectReference(nil), r.ImagePullSecrets...),
				SecurityContext:              &corev1.PodSecurityContext{RunAsNonRoot: &nonRoot, FSGroup: &group, SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}},
				Containers: []corev1.Container{{Name: "video-worker", Image: plan.WorkerImage, Command: command, Env: env,
					SecurityContext: &corev1.SecurityContext{AllowPrivilegeEscalation: &noEscalation, ReadOnlyRootFilesystem: &readOnly, Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}}},
					VolumeMounts:    []corev1.VolumeMount{{Name: "video-output", MountPath: videoTaskOutputMount}}}},
				Volumes: []corev1.Volume{{Name: "video-output", VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: plan.OutputClaimName}}}},
			},
		}},
	}
	if err := controllerutil.SetControllerReference(task, job, r.Scheme()); err != nil {
		return nil, err
	}
	return job, nil
}

// finishVideoTask publishes one terminal timestamp and schedules expiration without repeated writes.
func (r *VideoTaskReconciler) finishVideoTask(ctx context.Context, task *api.VideoTask) (ctrl.Result, error) {
	if task.Status.FinishedAt == nil {
		now := metav1.Now()
		task.Status.FinishedAt = &now
	}
	if task.Status.ExpiresAt == nil {
		retention := int64(0)
		if task.Status.Plan != nil {
			retention = task.Status.Plan.RetentionSeconds
		} else {
			var frontends api.FrontendServiceList
			if err := r.APIReader.List(ctx, &frontends, client.InNamespace(task.Namespace)); err != nil {
				return ctrl.Result{}, err
			}
			for i := range frontends.Items {
				if string(frontends.Items[i].UID) == task.Spec.FrontendUID && frontends.Items[i].Spec.VideoTasks != nil {
					retention = frontends.Items[i].Spec.VideoTasks.RetentionSeconds
					break
				}
			}
		}
		if retention <= 0 {
			return ctrl.Result{}, r.Delete(ctx, task)
		}
		expires := metav1.NewTime(task.Status.FinishedAt.Add(time.Duration(retention) * time.Second))
		task.Status.ExpiresAt = &expires
	}
	return ctrl.Result{RequeueAfter: time.Until(task.Status.ExpiresAt.Time)}, r.writeVideoTaskStatus(ctx, task)
}

// stopVideoWorker removes only the matching execution Job and waits for its Pods to leave the PVC.
func (r *VideoTaskReconciler) stopVideoWorker(ctx context.Context, task *api.VideoTask) (bool, error) {
	job := new(batchv1.Job)
	err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: task.Namespace, Name: task.Status.Plan.JobName}, job)
	if err != nil && !apierrors.IsNotFound(err) {
		return false, err
	}
	if err == nil && metav1.IsControlledBy(job, task) && (task.Status.JobUID == "" || task.Status.JobUID == string(job.UID)) {
		if err := r.Delete(ctx, job, client.PropagationPolicy(metav1.DeletePropagationForeground), client.Preconditions{UID: &job.UID}); err != nil && !apierrors.IsNotFound(err) {
			return false, err
		}
		return false, nil
	}
	var pods corev1.PodList
	if err := r.APIReader.List(ctx, &pods, client.InNamespace(task.Namespace), client.MatchingLabels{"batch.kubernetes.io/job-name": task.Status.Plan.JobName}); err != nil {
		return false, err
	}
	for i := range pods.Items {
		for _, ref := range pods.Items[i].OwnerReferences {
			if ref.Kind == "Job" && ref.Name == task.Status.Plan.JobName && (task.Status.JobUID == "" || ref.UID == types.UID(task.Status.JobUID)) {
				return false, nil
			}
		}
	}
	return true, nil
}

// cleanupVideoTask stops generation, runs storage cleanup, then releases the finalizer or expired task.
func (r *VideoTaskReconciler) cleanupVideoTask(ctx context.Context, task *api.VideoTask) (ctrl.Result, error) {
	if task.Status.Plan == nil {
		// An unverified task has no authority to select a PVC for cleanup.
		frontend, err := r.verifyVideoTaskFrontend(ctx, task)
		if err != nil {
			if !task.DeletionTimestamp.IsZero() {
				base := task.DeepCopy()
				controllerutil.RemoveFinalizer(task, videoTaskFinalizer)
				return ctrl.Result{}, r.Patch(ctx, task, client.MergeFrom(base))
			}
			return ctrl.Result{}, r.Delete(ctx, task)
		}
		task.Status.Plan = &api.VideoExecutionPlan{
			JobName: task.Name, OutputClaimName: frontend.Spec.VideoTasks.ClaimName,
			OutputPath:  fmt.Sprintf("tasks/%s/result.mp4", task.Name),
			WorkerImage: r.WorkerImage, RetentionSeconds: frontend.Spec.VideoTasks.RetentionSeconds,
		}
		if timeout, err := durationSeconds(frontend.Spec.Timeouts.Request); err == nil {
			task.Status.Plan.TimeoutSeconds = timeout
		} else {
			return ctrl.Result{}, err
		}
		return ctrl.Result{Requeue: true}, r.writeVideoTaskStatus(ctx, task)
	}
	done, err := r.stopVideoWorker(ctx, task)
	if err != nil || !done {
		return ctrl.Result{RequeueAfter: 2 * time.Second}, err
	}
	if task.Status.CleanupJobName == "" {
		task.Status.CleanupJobName = task.Name + "-cleanup"
		return ctrl.Result{Requeue: true}, r.writeVideoTaskStatus(ctx, task)
	}
	job := new(batchv1.Job)
	err = r.APIReader.Get(ctx, client.ObjectKey{Namespace: task.Namespace, Name: task.Status.CleanupJobName}, job)
	if apierrors.IsNotFound(err) {
		job, err = r.newVideoWorkerJob(task, *task.Status.Plan, true)
		if err != nil {
			return ctrl.Result{}, err
		}
		if err := r.Create(ctx, job); err != nil {
			if apierrors.IsAlreadyExists(err) {
				return ctrl.Result{Requeue: true}, nil
			}
			return ctrl.Result{}, err
		}
		return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
	}
	if err != nil {
		return ctrl.Result{}, err
	}
	if !metav1.IsControlledBy(job, task) {
		return ctrl.Result{}, fmt.Errorf("cleanup Job %s is not controlled by VideoTask", job.Name)
	}
	for _, condition := range job.Status.Conditions {
		if condition.Type == batchv1.JobComplete && condition.Status == corev1.ConditionTrue {
			if !task.DeletionTimestamp.IsZero() {
				base := task.DeepCopy()
				controllerutil.RemoveFinalizer(task, videoTaskFinalizer)
				return ctrl.Result{}, r.Patch(ctx, task, client.MergeFrom(base))
			}
			return ctrl.Result{}, r.Delete(ctx, task)
		}
		if condition.Type == batchv1.JobFailed && condition.Status == corev1.ConditionTrue {
			return ctrl.Result{}, fmt.Errorf("cleanup Job %s failed: %s", job.Name, condition.Message)
		}
	}
	return ctrl.Result{RequeueAfter: 2 * time.Second}, nil
}

// writeVideoTaskStatus publishes changed task state without generating no-op status events.
func (r *VideoTaskReconciler) writeVideoTaskStatus(ctx context.Context, task *api.VideoTask) error {
	// A fresh read supplies the original status for the merge patch.
	current := new(api.VideoTask)
	if err := r.APIReader.Get(ctx, client.ObjectKeyFromObject(task), current); err != nil {
		return err
	}
	if task.ResourceVersion != current.ResourceVersion || task.UID != current.UID {
		return apierrors.NewConflict(api.GroupVersion.WithResource("videotasks").GroupResource(), task.Name, fmt.Errorf("VideoTask changed during reconciliation"))
	}
	task.Status.ObservedGeneration = task.Generation
	if reflect.DeepEqual(current.Status, task.Status) {
		return nil
	}
	return r.Status().Patch(ctx, task, client.MergeFromWithOptions(current, client.MergeFromWithOptimisticLock{}))
}

func videoTaskTerminal(phase string) bool {
	return phase == "Succeeded" || phase == "Failed" || phase == "Cancelled"
}

func ptrInt32(value int32) *int32 { return &value }
