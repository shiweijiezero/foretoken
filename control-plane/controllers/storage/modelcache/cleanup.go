// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Retires collection Jobs, PVCs, and Retain PVs after their processes exit.

package modelcache

import (
	"context"
	"fmt"

	batchv1 "k8s.io/api/batch/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// cleanupCollections resumes cleanup for released or replaced sources, including PV-only leftovers.
func (reconciler *Reconciler) cleanupCollections(ctx context.Context, key client.ObjectKey, retain types.UID) (ctrl.Result, error) {
	collections := make(map[types.UID]bool)
	add := func(object client.Object) {
		annotations := object.GetAnnotations()
		uid := types.UID(object.GetLabels()[CollectionLabel])
		if uid != "" && uid != retain && annotations[modelCacheCollectionNamespaceAnnotation] == key.Namespace && annotations[modelCacheCollectionNameAnnotation] == key.Name &&
			annotations[modelCacheCollectionOwnerAnnotation] == reconciler.Namespace+"/"+reconciler.ServiceAccount {
			collections[uid] = true
		}
	}
	var jobs batchv1.JobList
	if err := reconciler.APIReader.List(ctx, &jobs, client.InNamespace(reconciler.Namespace), client.HasLabels{CollectionLabel}); err != nil {
		return ctrl.Result{}, err
	}
	for index := range jobs.Items {
		add(&jobs.Items[index])
	}
	var claims corev1.PersistentVolumeClaimList
	if err := reconciler.APIReader.List(ctx, &claims, client.InNamespace(reconciler.Namespace), client.HasLabels{CollectionLabel}); err != nil {
		return ctrl.Result{}, err
	}
	for index := range claims.Items {
		add(&claims.Items[index])
	}
	var volumes corev1.PersistentVolumeList
	if err := reconciler.APIReader.List(ctx, &volumes, client.HasLabels{CollectionLabel}); err != nil {
		return ctrl.Result{}, err
	}
	for index := range volumes.Items {
		add(&volumes.Items[index])
	}
	pending := false
	for uid := range collections {
		waiting, err := reconciler.retireCollection(ctx, key, uid)
		if err != nil {
			return ctrl.Result{}, err
		}
		pending = pending || waiting
	}
	if pending {
		return ctrl.Result{RequeueAfter: modelCacheCollectionPollInterval}, nil
	}
	return ctrl.Result{}, nil
}

// retireCollection retires Jobs, waits for consumers, then removes alias PVCs and PVs.
// Deletes use observed UIDs and resource versions; PV cleanup also requires the original claim UID.
func (reconciler *Reconciler) retireCollection(ctx context.Context, record client.ObjectKey, uid types.UID) (bool, error) {
	job := new(batchv1.Job)
	if err := reconciler.APIReader.Get(ctx, client.ObjectKey{Namespace: reconciler.Namespace, Name: modelCacheCollectionJobPrefix + string(uid)}, job); err == nil {
		if err := reconciler.validateCollectionResource(job, record, uid); err != nil {
			return false, err
		}
		if job.DeletionTimestamp.IsZero() {
			if err := reconciler.retireCollectionJob(ctx, job); err != nil {
				return false, err
			}
		}
		return true, nil
	} else if !apierrors.IsNotFound(err) {
		return false, err
	}
	name := modelCacheCollectionStoragePrefix + string(uid)
	var pods corev1.PodList
	if err := reconciler.APIReader.List(ctx, &pods, client.InNamespace(reconciler.Namespace)); err != nil {
		return false, err
	}
	for _, pod := range pods.Items {
		if pod.Status.Phase == corev1.PodSucceeded || pod.Status.Phase == corev1.PodFailed {
			continue
		}
		for _, volume := range pod.Spec.Volumes {
			if volume.PersistentVolumeClaim != nil && volume.PersistentVolumeClaim.ClaimName == name {
				return true, nil
			}
		}
	}
	claim := new(corev1.PersistentVolumeClaim)
	if err := reconciler.APIReader.Get(ctx, client.ObjectKey{Namespace: reconciler.Namespace, Name: name}, claim); err == nil {
		if err := reconciler.validateCollectionResource(claim, record, uid); err != nil {
			return false, err
		}
		if claim.Spec.VolumeName != name {
			return false, fmt.Errorf("cache collection claim %q no longer names its dedicated volume", name)
		}
		volume := new(corev1.PersistentVolume)
		if err := reconciler.APIReader.Get(ctx, client.ObjectKey{Name: name}, volume); err == nil {
			if err := reconciler.validateCollectionResource(volume, record, uid); err != nil {
				return false, err
			}
			binding := volume.Spec.ClaimRef
			if volume.Annotations[modelCacheCollectionClaimUIDAnnotation] != string(claim.UID) || binding == nil || binding.Namespace != reconciler.Namespace || binding.Name != claim.Name || binding.UID != claim.UID {
				return false, fmt.Errorf("cache collection claim %q no longer matches its volume's claim UID", name)
			}
		} else if !apierrors.IsNotFound(err) {
			return false, err
		}
		if claim.DeletionTimestamp.IsZero() {
			if err := reconciler.Delete(ctx, claim, client.Preconditions{UID: &claim.UID, ResourceVersion: &claim.ResourceVersion}); client.IgnoreNotFound(err) != nil {
				return false, fmt.Errorf("delete model cache collection claim: %w", err)
			}
		}
		return true, nil
	} else if !apierrors.IsNotFound(err) {
		return false, err
	}
	volume := new(corev1.PersistentVolume)
	if err := reconciler.APIReader.Get(ctx, client.ObjectKey{Name: name}, volume); apierrors.IsNotFound(err) {
		return false, nil
	} else if err != nil {
		return false, err
	}
	if err := reconciler.validateCollectionResource(volume, record, uid); err != nil {
		return false, err
	}
	binding := volume.Spec.ClaimRef
	claimUID := types.UID(volume.Annotations[modelCacheCollectionClaimUIDAnnotation])
	if claimUID == "" || binding == nil || binding.Namespace != reconciler.Namespace || binding.Name != name || binding.UID != claimUID || volume.Spec.PersistentVolumeReclaimPolicy != corev1.PersistentVolumeReclaimRetain {
		return false, fmt.Errorf("cache collection volume %q no longer retains its original claim UID", name)
	}
	if volume.DeletionTimestamp.IsZero() {
		if err := reconciler.Delete(ctx, volume, client.Preconditions{UID: &volume.UID, ResourceVersion: &volume.ResourceVersion}); client.IgnoreNotFound(err) != nil {
			return false, fmt.Errorf("delete model cache collection volume: %w", err)
		}
	}
	return true, nil
}
