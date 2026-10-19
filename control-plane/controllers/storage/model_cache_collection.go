// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Coordinates shared model-cache collection after preparation and serving consumers retire.

package storage

import (
	"context"
	"encoding/json"
	"fmt"
	"reflect"
	"slices"
	"time"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
)

const (
	modelCacheCollectionPollInterval = time.Second
	// ModelCacheCollectionLabel associates collection resources with an immutable source-record UID.
	ModelCacheCollectionLabel                  = "inference.foretoken.io/model-cache-collection"
	modelCacheCollectionNamespaceAnnotation    = "inference.foretoken.io/model-cache-source-namespace"
	modelCacheCollectionNameAnnotation         = "inference.foretoken.io/model-cache-source-name"
	modelCacheCollectionOwnerAnnotation        = "inference.foretoken.io/model-cache-collector"
	modelCacheCollectionSourceVolumeAnnotation = "inference.foretoken.io/model-cache-source-volume"
	modelCacheCollectionClaimUIDAnnotation     = "inference.foretoken.io/model-cache-claim-uid"
	modelCacheCollectedAnnotation              = "inference.foretoken.io/model-cache-collected"
	modelCacheCollectionEnv                    = "FORETOKEN_CACHE_COLLECTION"
	modelCacheCollectionJobPrefix              = "collect-model-cache-"
	modelCacheCollectionStoragePrefix          = "model-cache-gc-"
)

type modelCacheCollectionRecord struct {
	Namespace string    `json:"namespace"`
	Name      string    `json:"name"`
	UID       types.UID `json:"uid"`
}

type modelCacheCollectionVolume struct {
	Name     string                        `json:"name"`
	UID      types.UID                     `json:"uid"`
	ClaimRef corev1.ObjectReference        `json:"claimRef"`
	Source   corev1.PersistentVolumeSource `json:"source"`
}

type modelCacheCollectionAlias struct {
	Name      string    `json:"name"`
	UID       types.UID `json:"uid"`
	ClaimName string    `json:"claimName"`
	ClaimUID  types.UID `json:"claimUID"`
}

type modelCacheCollectionPlan struct {
	Version          int                               `json:"version"`
	Record           modelCacheCollectionRecord        `json:"record"`
	Cache            runtimeconfig.ModelCacheReference `json:"cache"`
	Volume           modelCacheCollectionVolume        `json:"volume"`
	CollectionVolume modelCacheCollectionAlias         `json:"collectionVolume"`
}

// ModelCacheCollectionReconciler owns platform collection Jobs and volume aliases for deleted sources.
// Workers authorize deletion from live API references under the shared filesystem lock.
type ModelCacheCollectionReconciler struct {
	client.Client
	APIReader            client.Reader
	Namespace            string
	ServiceAccount       string
	WorkerImage          string
	WorkerApplicationURL string
	DeviceResourceName   string
	ApplicationFiles     runtimeconfig.ApplicationFiles
	ImagePullSecrets     []corev1.LocalObjectReference
}

// SetupWithManager registers source and collection-resource watches with the platform manager.
func (reconciler *ModelCacheCollectionReconciler) SetupWithManager(manager ctrl.Manager) error {
	reconciler.APIReader = manager.GetAPIReader()
	return ctrl.NewControllerManagedBy(manager).
		Named("model-cache-collection").
		For(&corev1.ConfigMap{}).
		Watches(&batchv1.Job{}, handler.EnqueueRequestsFromMapFunc(reconciler.sourceForCollectionResource)).
		Watches(&corev1.PersistentVolumeClaim{}, handler.EnqueueRequestsFromMapFunc(reconciler.sourceForCollectionResource)).
		Watches(&corev1.PersistentVolume{}, handler.EnqueueRequestsFromMapFunc(reconciler.sourceForCollectionResource)).
		Complete(reconciler)
}

// sourceForCollectionResource requeues sources when collection resources change or are rediscovered.
func (reconciler *ModelCacheCollectionReconciler) sourceForCollectionResource(_ context.Context, object client.Object) []reconcile.Request {
	annotations := object.GetAnnotations()
	namespace, name := annotations[modelCacheCollectionNamespaceAnnotation], annotations[modelCacheCollectionNameAnnotation]
	if namespace == "" || name == "" {
		return nil
	}
	return []reconcile.Request{{NamespacedName: client.ObjectKey{Namespace: namespace, Name: name}}}
}

// Reconcile coordinates collection and releases the source record after its dedicated storage is retired.
func (reconciler *ModelCacheCollectionReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	record := new(corev1.ConfigMap)
	if err := reconciler.APIReader.Get(ctx, request.NamespacedName, record); apierrors.IsNotFound(err) {
		return reconciler.cleanupCollections(ctx, request.NamespacedName, "")
	} else if err != nil {
		return ctrl.Result{}, err
	}
	if record.Labels[runtimeconfig.ModelCacheSourceLabel] == "" || record.DeletionTimestamp.IsZero() || !controllerutil.ContainsFinalizer(record, runtimeconfig.ModelCacheReferenceFinalizer) {
		return reconciler.cleanupCollections(ctx, request.NamespacedName, "")
	}
	if result, err := reconciler.cleanupCollections(ctx, request.NamespacedName, record.UID); err != nil || result.RequeueAfter != 0 {
		return result, err
	}
	// Persist completion before retiring the Job and volumes so restarts do not repeat collection.
	if record.Annotations[modelCacheCollectedAnnotation] != "" {
		pending, err := reconciler.retireCollection(ctx, request.NamespacedName, record.UID)
		if err != nil {
			return ctrl.Result{}, err
		}
		if pending {
			return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, nil
		}
		base := record.DeepCopy()
		controllerutil.RemoveFinalizer(record, runtimeconfig.ModelCacheReferenceFinalizer)
		return ctrl.Result{}, reconciler.Patch(ctx, record, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	}
	plan, err := collectionSourcePlan(record)
	if err != nil {
		return ctrl.Result{}, err
	}
	job := new(batchv1.Job)
	key := client.ObjectKey{Namespace: reconciler.Namespace, Name: modelCacheCollectionJobPrefix + string(record.UID)}
	if err := reconciler.APIReader.Get(ctx, key, job); err == nil {
		if record.Data[runtimeconfig.PreparationRecordKey] == "" {
			if err := reconciler.validateCollectionResource(job, request.NamespacedName, record.UID); err != nil {
				return ctrl.Result{}, err
			}
			if job.DeletionTimestamp.IsZero() {
				if err := reconciler.retireCollectionJob(ctx, job); err != nil {
					return ctrl.Result{}, err
				}
			}
			return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, nil
		}
		return reconciler.observeCollectionJob(ctx, record, plan, job)
	} else if !apierrors.IsNotFound(err) {
		return ctrl.Result{}, fmt.Errorf("read model cache collection Job: %w", err)
	}

	// Source deletion requests collection; consumer observations cannot authorize file removal.
	consumers, err := reconciler.collectionConsumersExist(ctx, record, plan.Cache)
	if err != nil {
		return ctrl.Result{}, err
	}
	if consumers {
		return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, nil
	}
	// Acquisition publishes source identity before writing model files.
	if record.Data[runtimeconfig.PreparationRecordKey] == "" {
		return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, reconciler.recordCollectionComplete(ctx, record, "unpublished")
	}
	storage, err := reconciler.reconcileCollectionStorage(ctx, &plan)
	if err != nil {
		return ctrl.Result{}, err
	}
	switch storage {
	case collectionStorageManaged:
		return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, reconciler.recordCollectionComplete(ctx, record, "storage-managed")
	case collectionStoragePending:
		return ctrl.Result{RequeueAfter: runtimeCachePollInterval}, nil
	}
	desired, err := reconciler.collectionJob(plan)
	if err != nil {
		return ctrl.Result{}, err
	}
	if err := reconciler.Create(ctx, desired); err != nil {
		return ctrl.Result{}, fmt.Errorf("create model cache collection Job: %w", err)
	}
	return ctrl.Result{}, nil
}

// collectionSourcePlan projects record and storage identity; the worker reads current fixed versions.
func collectionSourcePlan(record *corev1.ConfigMap) (modelCacheCollectionPlan, error) {
	plan := modelCacheCollectionPlan{
		Version: 1,
		Record:  modelCacheCollectionRecord{Namespace: record.Namespace, Name: record.Name, UID: record.UID},
	}
	if err := json.Unmarshal([]byte(record.Data[runtimeconfig.ModelCacheReferenceKey]), &plan.Cache); err != nil {
		return plan, fmt.Errorf("decode model cache reference %s/%s: %w", record.Namespace, record.Name, err)
	}
	cache := plan.Cache
	if cache.ClaimName == "" || cache.ClaimUID == "" {
		return plan, fmt.Errorf("model cache reference %s/%s requires claimName and claimUID", record.Namespace, record.Name)
	}
	return plan, (runtimeconfig.RuntimeCacheProfile{MountPath: cache.MountPath}).Validate()
}

// collectionConsumersExist checks cohorts, preparation Jobs and Pods before source retirement.
func (reconciler *ModelCacheCollectionReconciler) collectionConsumersExist(ctx context.Context, record *corev1.ConfigMap, cache runtimeconfig.ModelCacheReference) (bool, error) {
	namespace := new(corev1.Namespace)
	err := reconciler.APIReader.Get(ctx, client.ObjectKey{Name: record.Namespace}, namespace)
	if err != nil && !apierrors.IsNotFound(err) {
		return false, fmt.Errorf("read model cache source namespace: %w", err)
	}
	terminating := apierrors.IsNotFound(err) || !namespace.DeletionTimestamp.IsZero()
	if !terminating {
		var groups inferencev1alpha1.ModelGroupList
		if err := reconciler.APIReader.List(ctx, &groups, client.InNamespace(record.Namespace)); err != nil {
			return false, fmt.Errorf("read model cache source Groups: %w", err)
		}
		for _, group := range groups.Items {
			if runtimeconfig.PreparationSourceName(group.Spec) == record.Name {
				return true, nil
			}
		}
	}
	// Native Jobs can still launch acquisition Pods after namespace controllers exit.
	var jobs batchv1.JobList
	if err := reconciler.APIReader.List(ctx, &jobs, client.InNamespace(record.Namespace)); err != nil {
		return false, fmt.Errorf("read model cache source preparation Jobs: %w", err)
	}
	for _, job := range jobs.Items {
		if !podReferencesPreparationSource(&job.Spec.Template.Spec, record.Name) {
			continue
		}
		terminal := false
		for _, condition := range job.Status.Conditions {
			if condition.Status == corev1.ConditionTrue && (condition.Type == batchv1.JobComplete || condition.Type == batchv1.JobFailed) {
				terminal = true
				break
			}
		}
		if !terminal {
			return true, nil
		}
	}
	var pods corev1.PodList
	if err := reconciler.APIReader.List(ctx, &pods, client.InNamespace(record.Namespace)); err != nil {
		return false, fmt.Errorf("read model cache source consumers: %w", err)
	}
	for _, pod := range pods.Items {
		if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		if podReferencesPreparationSource(&pod.Spec, record.Name) {
			return true, nil
		}
		if terminating {
			for _, volume := range pod.Spec.Volumes {
				if volume.PersistentVolumeClaim != nil && volume.PersistentVolumeClaim.ClaimName == cache.ClaimName {
					return true, nil
				}
			}
		}
	}
	return false, nil
}

// podReferencesPreparationSource identifies acquisition and serving consumers of one source record.
func podReferencesPreparationSource(pod *corev1.PodSpec, name string) bool {
	for _, volume := range pod.Volumes {
		if volume.ConfigMap != nil && volume.ConfigMap.Name == name {
			return true
		}
	}
	for _, container := range append(slices.Clone(pod.InitContainers), pod.Containers...) {
		for _, variable := range container.Env {
			if variable.Name == runtimeconfig.PreparationConfigMapEnv && variable.Value == name {
				return true
			}
		}
	}
	return false
}

// observeCollectionJob validates execution identity and persists success; failed Jobs retain storage for retry.
func (reconciler *ModelCacheCollectionReconciler) observeCollectionJob(ctx context.Context, record *corev1.ConfigMap, plan modelCacheCollectionPlan, job *batchv1.Job) (ctrl.Result, error) {
	if err := reconciler.validateCollectionResource(job, client.ObjectKeyFromObject(record), record.UID); err != nil {
		return ctrl.Result{}, err
	}
	if !job.DeletionTimestamp.IsZero() {
		return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, nil
	}
	var submitted modelCacheCollectionPlan
	for _, container := range job.Spec.Template.Spec.Containers {
		for _, variable := range container.Env {
			if variable.Name == modelCacheCollectionEnv {
				if err := json.Unmarshal([]byte(variable.Value), &submitted); err != nil {
					return ctrl.Result{}, fmt.Errorf("decode submitted model cache collection plan: %w", err)
				}
			}
		}
	}
	if submitted.Version != plan.Version || submitted.Record != plan.Record || !reflect.DeepEqual(submitted.Cache, plan.Cache) {
		return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, reconciler.retireCollectionJob(ctx, job)
	}
	for _, condition := range job.Status.Conditions {
		if condition.Status != corev1.ConditionTrue {
			continue
		}
		switch condition.Type {
		case batchv1.JobComplete:
			return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, reconciler.recordCollectionComplete(ctx, record, string(job.UID))
		case batchv1.JobFailed:
			// Capture termination diagnostics before the failed Job and its Pods are removed.
			message := fmt.Sprintf("model cache collection Job %q failed: %s: %s", job.Name, condition.Reason, condition.Message)
			var pods corev1.PodList
			if err := reconciler.APIReader.List(ctx, &pods, client.InNamespace(job.Namespace), client.MatchingLabels{"batch.kubernetes.io/controller-uid": string(job.UID)}); err != nil {
				return ctrl.Result{}, fmt.Errorf("%s; read worker diagnostics: %w", message, err)
			}
			for _, pod := range pods.Items {
				for _, status := range append(slices.Clone(pod.Status.InitContainerStatuses), pod.Status.ContainerStatuses...) {
					if ended := status.State.Terminated; ended != nil && ended.ExitCode != 0 {
						message += fmt.Sprintf("\n%s/%s: %s (exit %d): %s", pod.Name, status.Name, ended.Reason, ended.ExitCode, ended.Message)
					}
				}
			}
			if err := reconciler.retireCollectionJob(ctx, job); err != nil {
				return ctrl.Result{}, fmt.Errorf("%s; retire failed worker: %w", message, err)
			}
			return ctrl.Result{}, fmt.Errorf("%s", message)
		}
	}
	return ctrl.Result{}, nil
}

// recordCollectionComplete persists cleanup without discarding concurrent source publication.
func (reconciler *ModelCacheCollectionReconciler) recordCollectionComplete(ctx context.Context, record *corev1.ConfigMap, completion string) error {
	base := record.DeepCopy()
	if record.Annotations == nil {
		record.Annotations = make(map[string]string)
	}
	record.Annotations[modelCacheCollectedAnnotation] = completion
	if err := reconciler.Patch(ctx, record, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		return fmt.Errorf("record model cache cleanup phase: %w", err)
	}
	return nil
}

// retireCollectionJob deletes the observed Job version and lets Kubernetes retire its Pods.
func (reconciler *ModelCacheCollectionReconciler) retireCollectionJob(ctx context.Context, job *batchv1.Job) error {
	return client.IgnoreNotFound(reconciler.Delete(ctx, job, client.PropagationPolicy("Foreground"), client.Preconditions{UID: &job.UID, ResourceVersion: &job.ResourceVersion}))
}
