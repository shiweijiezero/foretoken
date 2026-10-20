// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Writes KV-owned infrastructure while preserving recorded retention.

package kv

import (
	"context"
	"fmt"
	"maps"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	networkingv1 "k8s.io/api/networking/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

// applyOwnedResource persists KVService and KVGroup intent and returns the API response through desired.
// Allocated fields and metadata remain intact; PVC retention stays with the choice recorded at creation.
func applyOwnedResource(ctx context.Context, kubeClient client.Client, owner, desired client.Object, fieldOwner string) error {
	gvk, err := kubeClient.GroupVersionKindFor(desired)
	if err != nil {
		return err
	}
	object, err := kubeClient.Scheme().New(gvk)
	if err != nil {
		return err
	}
	// JSON decoding preserves omitted fields on a reused target; intent must not seed the merge base.
	current := object.(client.Object)
	err = kubeClient.Get(ctx, client.ObjectKeyFromObject(desired), current)
	missing := apierrors.IsNotFound(err)
	if err != nil && !missing {
		return err
	}
	if missing {
		if err := controllerutil.SetControllerReference(owner, desired, kubeClient.Scheme()); err != nil {
			return err
		}
		return kubeClient.Create(ctx, desired, client.FieldOwner(fieldOwner))
	}
	if !metav1.IsControlledBy(current, owner) {
		return fmt.Errorf("%T %q is not controlled by %T %q", current, current.GetName(), owner, owner.GetName())
	}
	labels, annotations := desired.GetLabels(), desired.GetAnnotations()
	if pvc, ok := desired.(*corev1.PersistentVolumeClaim); ok {
		existing := current.(*corev1.PersistentVolumeClaim)
		switch owner.(type) {
		case *inferencev1alpha1.KVService:
			if retention, recorded := existing.Annotations[snapshotRetentionAnnotation]; recorded {
				pvc.Annotations[snapshotRetentionAnnotation] = retention
			}
		case *inferencev1alpha1.KVGroup:
			if retention := existing.Annotations[kvGroupDiskRetentionAnnotation]; retention != "" {
				pvc.Annotations[kvGroupDiskRetentionAnnotation] = retention
			}
		}
	}

	// Merge patches replace owned lists, including old numerical port keys and volume sources.
	// Apply cannot remove omitted items that an earlier Create or Update still owns.
	switch object := desired.(type) {
	case *corev1.Service:
		spec := object.Spec
		current.(*corev1.Service).DeepCopyInto(object)
		object.Spec.Ports = spec.Ports
		object.Spec.Selector = spec.Selector
		object.Spec.PublishNotReadyAddresses = spec.PublishNotReadyAddresses
		if spec.Type != "" {
			object.Spec.Type = spec.Type
		}
	case *corev1.PersistentVolumeClaim:
		spec := object.Spec
		current.(*corev1.PersistentVolumeClaim).DeepCopyInto(object)
		object.Spec.AccessModes = spec.AccessModes
		object.Spec.Resources.Requests = spec.Resources.Requests
		if spec.StorageClassName != nil {
			object.Spec.StorageClassName = spec.StorageClassName
		}
	case *corev1.ConfigMap:
		data, binaryData := object.Data, object.BinaryData
		current.(*corev1.ConfigMap).DeepCopyInto(object)
		object.Data, object.BinaryData = data, binaryData
	case *appsv1.Deployment:
		spec := object.Spec
		current.(*appsv1.Deployment).DeepCopyInto(object)
		object.Spec = spec
	case *appsv1.StatefulSet:
		spec := object.Spec
		current.(*appsv1.StatefulSet).DeepCopyInto(object)
		object.Spec = spec
	case *networkingv1.NetworkPolicy:
		spec := object.Spec
		current.(*networkingv1.NetworkPolicy).DeepCopyInto(object)
		object.Spec = spec
	default:
		return fmt.Errorf("unsupported KV resource %T", desired)
	}
	if len(labels) > 0 {
		if desired.GetLabels() == nil {
			desired.SetLabels(make(map[string]string, len(labels)))
		}
		maps.Copy(desired.GetLabels(), labels)
	}
	if len(annotations) > 0 {
		if desired.GetAnnotations() == nil {
			desired.SetAnnotations(make(map[string]string, len(annotations)))
		}
		maps.Copy(desired.GetAnnotations(), annotations)
	}
	if err := controllerutil.SetControllerReference(owner, desired, kubeClient.Scheme()); err != nil {
		return err
	}
	return kubeClient.Patch(ctx, desired, client.MergeFromWithOptions(current, client.MergeFromWithOptimisticLock{}), client.FieldOwner(fieldOwner))
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
