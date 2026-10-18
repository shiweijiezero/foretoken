// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Writes KV-owned infrastructure while preserving Kubernetes allocations and recorded retention.

package kv

import (
	"context"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// applyOwnedResource writes one KVService or KVGroup resource without adopting another owner's infrastructure.
// Workloads use their controller's field owner; other resources retain allocated fields and recorded retention.
func applyOwnedResource(ctx context.Context, kubeClient client.Client, owner, desired client.Object, fieldOwner string) error {
	current := desired.DeepCopyObject().(client.Object)
	err := kubeClient.Get(ctx, client.ObjectKeyFromObject(desired), current)
	missing := apierrors.IsNotFound(err)
	if err != nil && !missing {
		return err
	}
	if err := controllerutil.SetControllerReference(owner, desired, kubeClient.Scheme()); err != nil {
		return err
	}
	if !missing && !metav1.IsControlledBy(current, owner) {
		return fmt.Errorf("%T %q is not controlled by %s", current, current.GetName(), metav1.GetControllerOf(desired).Kind)
	}
	switch desired.(type) {
	case *appsv1.Deployment, *appsv1.StatefulSet:
		// Workload controllers retain their revision metadata independently of KV-owned spec fields.
		return kubeClient.Patch(ctx, desired, client.Apply, client.FieldOwner(fieldOwner), client.ForceOwnership)
	}
	if missing {
		return kubeClient.Create(ctx, desired)
	}
	if desiredPVC, ok := desired.(*corev1.PersistentVolumeClaim); ok {
		currentPVC := current.(*corev1.PersistentVolumeClaim)
		preservePVCBindingAndMetadata(desiredPVC, currentPVC)
		switch owner.(type) {
		case *inferencev1alpha1.KVService:
			if retention, recorded := currentPVC.Annotations[snapshotRetentionAnnotation]; recorded {
				desiredPVC.Annotations[snapshotRetentionAnnotation] = retention
			}
		case *inferencev1alpha1.KVGroup:
			if retention := currentPVC.Annotations[kvGroupDiskRetentionAnnotation]; retention != "" {
				desiredPVC.Annotations[kvGroupDiskRetentionAnnotation] = retention
			}
		}
	}
	if desiredService, ok := desired.(*corev1.Service); ok {
		currentService := current.(*corev1.Service)
		desiredService.Spec.ClusterIP = currentService.Spec.ClusterIP
		desiredService.Spec.ClusterIPs = currentService.Spec.ClusterIPs
		desiredService.Spec.IPFamilies = currentService.Spec.IPFamilies
		desiredService.Spec.IPFamilyPolicy = currentService.Spec.IPFamilyPolicy
	}
	desired.SetResourceVersion(current.GetResourceVersion())
	return kubeClient.Update(ctx, desired)
}

// preservePVCBindingAndMetadata keeps provider-assigned PVC fields and metadata during updates.
// Callers initialize desired.Annotations; resource owners retain capacity, StorageClass, and retention decisions.
func preservePVCBindingAndMetadata(desired, existing *corev1.PersistentVolumeClaim) {
	desired.Spec.VolumeName = existing.Spec.VolumeName
	desired.Spec.VolumeMode = existing.Spec.VolumeMode
	if desired.Spec.StorageClassName == nil {
		desired.Spec.StorageClassName = existing.Spec.StorageClassName
	}
	desired.Finalizers = existing.Finalizers
	for key, value := range existing.Annotations {
		if _, present := desired.Annotations[key]; !present {
			desired.Annotations[key] = value
		}
	}
}

// deleteIfPresent requests resource deletion and reports whether its removal still needs observation.
func deleteIfPresent(ctx context.Context, kubeClient client.Client, object client.Object) (bool, error) {
	if err := kubeClient.Get(ctx, client.ObjectKeyFromObject(object), object); err != nil {
		if apierrors.IsNotFound(err) {
			return false, nil
		}
		return false, err
	}
	if err := kubeClient.Delete(ctx, object); err != nil && !apierrors.IsNotFound(err) {
		return false, err
	}
	return true, nil
}
