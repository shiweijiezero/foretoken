// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Reconciles RuntimeCache resources into platform-managed persistent volumes.

package controllers

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
)

const (
	runtimeCacheFinalizer           = "inference.foretoken.io/runtimecache-protection"
	runtimeCacheLabel               = "inference.foretoken.io/runtime-cache"
	runtimeCacheRetentionAnnotation = "inference.foretoken.io/runtime-cache-retention"
)

// RuntimeCacheReconciler owns the PVC lifecycle for one RuntimeCache.
type RuntimeCacheReconciler struct{ client.Client }

// SetupWithManager registers RuntimeCache reconciliation and its owned PVC.
func (reconciler *RuntimeCacheReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		For(&inferencev1alpha1.RuntimeCache{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Complete(reconciler)
}

// Reconcile creates, expands, retains, or deletes the managed cache PVC.
func (reconciler *RuntimeCacheReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	cache := new(inferencev1alpha1.RuntimeCache)
	if err := reconciler.Get(ctx, request.NamespacedName, cache); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !cache.DeletionTimestamp.IsZero() {
		return reconciler.reconcileDelete(ctx, cache)
	}
	if !controllerutil.ContainsFinalizer(cache, runtimeCacheFinalizer) {
		base := cache.DeepCopy()
		controllerutil.AddFinalizer(cache, runtimeCacheFinalizer)
		if err := reconciler.Patch(ctx, cache, client.MergeFrom(base)); err != nil {
			return ctrl.Result{}, fmt.Errorf("add RuntimeCache finalizer: %w", err)
		}
		return ctrl.Result{Requeue: true}, nil
	}

	pvc, resizing, err := reconciler.reconcilePVC(ctx, cache)
	if err != nil {
		statusErr := reconciler.updateStatus(ctx, cache, inferencev1alpha1.RuntimeCachePhaseDegraded, false, "ApplyFailed", err.Error(), nil)
		return ctrl.Result{}, errors.Join(fmt.Errorf("reconcile RuntimeCache PVC: %w", err), statusErr)
	}
	ready := pvc.Status.Phase == corev1.ClaimBound && pvcCapacityAtLeastRequest(pvc)
	phase, reason, message := inferencev1alpha1.RuntimeCachePhasePending, "WaitingForBinding", "Runtime cache PVC is not bound"
	if resizing || pvc.Status.Phase == corev1.ClaimBound && !ready {
		phase, reason, message = inferencev1alpha1.RuntimeCachePhaseResizing, "Resizing", "Runtime cache PVC is expanding"
	}
	if ready {
		phase, reason, message = inferencev1alpha1.RuntimeCachePhaseReady, "Ready", "Runtime cache PVC is bound"
	}
	result := ctrl.Result{}
	if ready && cache.Spec.MaxSize != "" {
		state, err := reconciler.reconcileAutomaticExpansion(ctx, cache, pvc)
		if err != nil {
			statusErr := reconciler.updateStatus(ctx, cache, inferencev1alpha1.RuntimeCachePhaseDegraded, false, "ExpansionFailed", err.Error(), pvc)
			return ctrl.Result{}, errors.Join(fmt.Errorf("expand RuntimeCache PVC: %w", err), statusErr)
		}
		result.RequeueAfter = runtimeCachePollInterval
		if state.resizing {
			phase, ready, reason, message = inferencev1alpha1.RuntimeCachePhaseResizing, false, "Resizing", "Runtime cache PVC is expanding"
		}
		if state.degraded {
			phase, ready, reason, message = inferencev1alpha1.RuntimeCachePhaseDegraded, false, state.reason, state.message
		}
	}
	if err := reconciler.updateStatus(ctx, cache, phase, ready, reason, message, pvc); err != nil {
		return ctrl.Result{}, err
	}
	return result, nil
}

func runtimeCachePVCName(cache *inferencev1alpha1.RuntimeCache) string {
	identity := strings.ReplaceAll(string(cache.UID), "-", "")
	if identity == "" {
		identity = "cache"
	}
	if len(identity) > 32 {
		identity = identity[:32]
	}
	prefix := cache.Name + "-storage"
	if limit := 62 - len(identity); len(prefix) > limit {
		prefix = strings.TrimRight(prefix[:limit], "-.")
	}
	return prefix + "-" + identity
}

// desiredRuntimeCachePVC builds the immutable storage class, access mode, and retention contract.
func desiredRuntimeCachePVC(cache *inferencev1alpha1.RuntimeCache) (*corev1.PersistentVolumeClaim, error) {
	size, err := resource.ParseQuantity(string(cache.Spec.InitialSize))
	if err != nil || size.Sign() <= 0 {
		return nil, fmt.Errorf("runtime cache initialSize must be a positive Kubernetes quantity")
	}
	accessMode := corev1.PersistentVolumeAccessMode(cache.Spec.AccessMode)
	if accessMode == "" {
		accessMode = corev1.ReadWriteMany
	}
	retention := cache.Spec.RetentionPolicy
	if retention == "" {
		retention = inferencev1alpha1.RuntimeCacheRetentionPolicyRetain
	}
	pvc := &corev1.PersistentVolumeClaim{
		TypeMeta: metav1.TypeMeta{APIVersion: corev1.SchemeGroupVersion.String(), Kind: "PersistentVolumeClaim"},
		ObjectMeta: metav1.ObjectMeta{
			Name:        runtimeCachePVCName(cache),
			Namespace:   cache.Namespace,
			Labels:      map[string]string{runtimeCacheLabel: cache.Name},
			Annotations: map[string]string{runtimeCacheRetentionAnnotation: string(retention)},
		},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes: []corev1.PersistentVolumeAccessMode{accessMode},
			Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{
				corev1.ResourceStorage: size,
			}},
		},
	}
	if cache.Spec.StorageClassName != "" {
		pvc.Spec.StorageClassName = &cache.Spec.StorageClassName
	}
	return pvc, nil
}

// reconcilePVC creates the claim or increases its requested capacity without replacing it.
func (reconciler *RuntimeCacheReconciler) reconcilePVC(ctx context.Context, cache *inferencev1alpha1.RuntimeCache) (*corev1.PersistentVolumeClaim, bool, error) {
	desired, err := desiredRuntimeCachePVC(cache)
	if err != nil {
		return nil, false, err
	}
	if err := controllerutil.SetControllerReference(cache, desired, reconciler.Scheme()); err != nil {
		return nil, false, err
	}
	current := new(corev1.PersistentVolumeClaim)
	key := client.ObjectKeyFromObject(desired)
	if err := reconciler.Get(ctx, key, current); apierrors.IsNotFound(err) {
		if err := reconciler.Create(ctx, desired); err != nil {
			return nil, false, err
		}
		return desired, false, nil
	} else if err != nil {
		return nil, false, err
	}
	if !metav1.IsControlledBy(current, cache) {
		return nil, false, fmt.Errorf("PersistentVolumeClaim %q is not controlled by RuntimeCache", current.Name)
	}
	currentRequest := current.Spec.Resources.Requests[corev1.ResourceStorage]
	desiredRequest := desired.Spec.Resources.Requests[corev1.ResourceStorage]
	if currentRequest.Cmp(desiredRequest) >= 0 {
		return current, false, nil
	}
	base := current.DeepCopy()
	current.Spec.Resources.Requests[corev1.ResourceStorage] = desiredRequest
	if err := reconciler.Patch(ctx, current, client.MergeFrom(base)); err != nil {
		return nil, false, err
	}
	return current, true, nil
}

func pvcCapacityAtLeastRequest(pvc *corev1.PersistentVolumeClaim) bool {
	capacity := pvc.Status.Capacity[corev1.ResourceStorage]
	requested := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	return capacity.Cmp(requested) >= 0
}

type runtimeCacheExpansionState struct {
	resizing bool
	degraded bool
	reason   string
	message  string
}

// reconcileAutomaticExpansion doubles the current request when free space falls below the initial allocation.
func (reconciler *RuntimeCacheReconciler) reconcileAutomaticExpansion(ctx context.Context, cache *inferencev1alpha1.RuntimeCache, pvc *corev1.PersistentVolumeClaim) (runtimeCacheExpansionState, error) {
	minimumAvailable, err := runtimeCacheQuantityBytes(cache.Spec.InitialSize, "initialSize")
	if err != nil {
		return runtimeCacheExpansionState{}, err
	}
	maxSize, err := runtimeCacheQuantityBytes(cache.Spec.MaxSize, "maxSize")
	if err != nil {
		return runtimeCacheExpansionState{}, err
	}
	if minimumAvailable >= maxSize {
		return runtimeCacheExpansionState{}, fmt.Errorf("maxSize must be greater than initialSize")
	}
	observations, observationErr := observeRuntimeCache(ctx, reconciler.Client, cache, pvc.Name)
	if observationErr != nil {
		return runtimeCacheExpansionState{}, observationErr
	}
	if len(observations) == 0 {
		return runtimeCacheExpansionState{}, nil
	}
	capacity, available := observations[0].CapacityBytes, observations[0].AvailableBytes
	for _, observation := range observations[1:] {
		capacity = min(capacity, observation.CapacityBytes)
		available = min(available, observation.AvailableBytes)
	}
	currentQuantity := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	currentRequest := currentQuantity.Value()
	if available >= minimumAvailable {
		return runtimeCacheExpansionState{}, nil
	}
	used := capacity - available
	if used > maxSize-minimumAvailable {
		return runtimeCacheExpansionState{degraded: true, reason: "MaxSizeReached", message: "Runtime cache does not have enough space within maxSize"}, nil
	}
	required := used + minimumAvailable
	target := required
	if currentRequest <= maxSize/2 {
		target = max(target, currentRequest*2)
	} else {
		target = maxSize
	}
	target = min(target, maxSize)
	if target <= currentRequest {
		return runtimeCacheExpansionState{degraded: true, reason: "MaxSizeReached", message: "Runtime cache reached maxSize with insufficient free space"}, nil
	}
	base := pvc.DeepCopy()
	pvc.Spec.Resources.Requests[corev1.ResourceStorage] = *resource.NewQuantity(target, resource.BinarySI)
	if err := reconciler.Patch(ctx, pvc, client.MergeFrom(base)); err != nil {
		return runtimeCacheExpansionState{}, err
	}
	return runtimeCacheExpansionState{resizing: true}, nil
}

func runtimeCacheQuantityBytes(value inferencev1alpha1.ResourceQuantity, field string) (int64, error) {
	quantity, err := resource.ParseQuantity(string(value))
	if err != nil || quantity.Sign() <= 0 {
		return 0, fmt.Errorf("automatic expansion %s must be a positive Kubernetes quantity", field)
	}
	bytes := quantity.Value()
	if quantity.CmpInt64(bytes) != 0 {
		return 0, fmt.Errorf("automatic expansion %s must be an exact byte quantity", field)
	}
	return bytes, nil
}

// reconcileDelete releases retained storage or waits for Kubernetes to delete the claim safely.
func (reconciler *RuntimeCacheReconciler) reconcileDelete(ctx context.Context, cache *inferencev1alpha1.RuntimeCache) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(cache, runtimeCacheFinalizer) {
		return ctrl.Result{}, nil
	}
	pvc := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: runtimeCachePVCName(cache), Namespace: cache.Namespace}}
	if err := reconciler.Get(ctx, client.ObjectKeyFromObject(pvc), pvc); err != nil {
		if !apierrors.IsNotFound(err) {
			return ctrl.Result{}, err
		}
		return ctrl.Result{}, reconciler.removeFinalizer(ctx, cache)
	}
	retention := pvc.Annotations[runtimeCacheRetentionAnnotation]
	if retention == string(inferencev1alpha1.RuntimeCacheRetentionPolicyRetain) {
		if err := releaseRuntimeCachePVC(ctx, reconciler.Client, cache, pvc); err != nil {
			return ctrl.Result{}, err
		}
		return ctrl.Result{}, reconciler.removeFinalizer(ctx, cache)
	}
	if err := reconciler.updateStatus(ctx, cache, inferencev1alpha1.RuntimeCachePhaseTerminating, false, "Deleting", "Runtime cache PVC is deleting", pvc); err != nil {
		return ctrl.Result{}, err
	}
	// Kubernetes PVC protection delays removal while a Pod still mounts the claim.
	if err := reconciler.Delete(ctx, pvc); err != nil && !apierrors.IsNotFound(err) {
		return ctrl.Result{}, err
	}
	return ctrl.Result{Requeue: true}, nil
}

// releaseRuntimeCachePVC removes controller ownership while preserving the allocated volume.
func releaseRuntimeCachePVC(ctx context.Context, kubeClient client.Client, cache *inferencev1alpha1.RuntimeCache, pvc *corev1.PersistentVolumeClaim) error {
	base := pvc.DeepCopy()
	owners := pvc.OwnerReferences[:0]
	for _, owner := range pvc.OwnerReferences {
		if owner.UID != cache.UID {
			owners = append(owners, owner)
		}
	}
	pvc.OwnerReferences = owners
	if reflect.DeepEqual(base.OwnerReferences, pvc.OwnerReferences) {
		return nil
	}
	return kubeClient.Patch(ctx, pvc, client.MergeFrom(base))
}

func (reconciler *RuntimeCacheReconciler) removeFinalizer(ctx context.Context, cache *inferencev1alpha1.RuntimeCache) error {
	base := cache.DeepCopy()
	controllerutil.RemoveFinalizer(cache, runtimeCacheFinalizer)
	return reconciler.Patch(ctx, cache, client.MergeFrom(base))
}

// updateStatus publishes the managed claim identity, observed capacity, and readiness.
func (reconciler *RuntimeCacheReconciler) updateStatus(ctx context.Context, cache *inferencev1alpha1.RuntimeCache, phase inferencev1alpha1.RuntimeCachePhase, ready bool, reason, message string, pvc *corev1.PersistentVolumeClaim) error {
	base := cache.DeepCopy()
	cache.Status.ObservedGeneration = cache.Generation
	cache.Status.Phase = phase
	cache.Status.ClaimName = runtimeCachePVCName(cache)
	cache.Status.Capacity = ""
	if pvc != nil {
		if capacity, ok := pvc.Status.Capacity[corev1.ResourceStorage]; ok {
			cache.Status.Capacity = inferencev1alpha1.ResourceQuantity(capacity.String())
		}
	}
	meta.SetStatusCondition(&cache.Status.Conditions, metav1.Condition{Type: conditionReady, Status: conditionStatus(ready), Reason: reason, Message: message, ObservedGeneration: cache.Generation})
	if reflect.DeepEqual(base.Status, cache.Status) {
		return nil
	}
	return reconciler.Status().Patch(ctx, cache, client.MergeFrom(base))
}
