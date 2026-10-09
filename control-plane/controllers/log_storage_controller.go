// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Expands managed Loki storage from kubelet filesystem observations.

package controllers

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/predicate"
)

// LogStorageReconciler grows claims mounted by the CLI-selected Loki StatefulSet.
// Helm owns the workload and initial claim template; this controller only increases live claims.
type LogStorageReconciler struct {
	client.Client
	Kubernetes  *kubernetes.Clientset
	StatefulSet client.ObjectKey
	MaxSize     resource.Quantity
}

// SetupWithManager watches the selected log store; successful observations repeat every 30 seconds.
func (r *LogStorageReconciler) SetupWithManager(manager ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(manager).
		Named("log-storage").
		For(&appsv1.StatefulSet{}).
		WithEventFilter(predicate.NewPredicateFuncs(func(object client.Object) bool {
			return client.ObjectKeyFromObject(object) == r.StatefulSet
		})).
		Complete(r)
}

// Reconcile reads the mounted log filesystem and requests bounded growth through Kubernetes.
// Missing measurements and unsupported storage surface as reconciliation errors, not zero usage.
func (r *LogStorageReconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
	statefulSet := new(appsv1.StatefulSet)
	if err := r.Get(ctx, request.NamespacedName, statefulSet); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !statefulSet.DeletionTimestamp.IsZero() {
		return ctrl.Result{}, nil
	}
	selector, err := metav1.LabelSelectorAsSelector(statefulSet.Spec.Selector)
	if err != nil {
		return ctrl.Result{}, err
	}
	var pods corev1.PodList
	if err := r.List(ctx, &pods, client.InNamespace(statefulSet.Namespace), client.MatchingLabelsSelector{Selector: selector}); err != nil {
		return ctrl.Result{}, err
	}
	for i := range pods.Items {
		pod := &pods.Items[i]
		if !metav1.IsControlledBy(pod, statefulSet) || pod.Spec.NodeName == "" || pod.Status.Phase != corev1.PodRunning || !pod.DeletionTimestamp.IsZero() {
			continue
		}
		for _, template := range statefulSet.Spec.VolumeClaimTemplates {
			// StatefulSet supplies the exact mounted claim identity; do not reconstruct chart names.
			for _, volume := range pod.Spec.Volumes {
				if volume.Name != template.Name || volume.PersistentVolumeClaim == nil {
					continue
				}
				if err := r.expandClaim(ctx, pod, volume.PersistentVolumeClaim.ClaimName); err != nil {
					return ctrl.Result{}, err
				}
			}
		}
	}
	return ctrl.Result{RequeueAfter: 30 * time.Second}, nil
}

// expandClaim waits for pending resizes and uses live PVC requests to avoid repeating growth
// against a pre-expansion kubelet measurement. It never shrinks or replaces retained storage.
func (r *LogStorageReconciler) expandClaim(ctx context.Context, pod *corev1.Pod, name string) error {
	pvc := new(corev1.PersistentVolumeClaim)
	if err := r.Get(ctx, client.ObjectKey{Namespace: pod.Namespace, Name: name}, pvc); err != nil {
		return err
	}
	if !pvc.DeletionTimestamp.IsZero() || pvc.Status.Phase != corev1.ClaimBound {
		return nil
	}
	if message, failed := pvcResizeFailure(pvc); failed {
		return fmt.Errorf("log claim %s: %s", name, message)
	}
	if !pvcCapacityAtLeastRequest(pvc) || pvcResizePending(pvc) {
		return nil
	}
	current := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	if current.Cmp(r.MaxSize) >= 0 {
		return nil
	}
	capacity := pvc.Status.Capacity[corev1.ResourceStorage]
	used, err := r.filesystem(ctx, pod, name, capacity.Value())
	if err != nil {
		return err
	}
	requested := current.Value()
	if used < requested-requested/pvcExpansionFreeSpaceDivisor {
		return nil
	}
	target := nextPVCSize(requested, r.MaxSize.Value())
	base := pvc.DeepCopy()
	pvc.Spec.Resources.Requests[corev1.ResourceStorage] = *resource.NewQuantity(target, resource.BinarySI)
	if err := r.Patch(ctx, pvc, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{})); err != nil {
		return err
	}
	ctrl.LoggerFrom(ctx).Info("expanding log storage", "claim", client.ObjectKeyFromObject(pvc), "bytes", target)
	return nil
}

// filesystem reads kubelet's public summary API through the authenticated Kubernetes API server.
// Pod UID and PVC identity bind observations to the current mounted log volume.
func (r *LogStorageReconciler) filesystem(ctx context.Context, pod *corev1.Pod, claim string, allocatedBytes int64) (int64, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	data, err := r.Kubernetes.CoreV1().RESTClient().Get().Resource("nodes").Name(pod.Spec.NodeName).
		SubResource("proxy").Suffix("stats", "summary").DoRaw(ctx)
	if err != nil {
		return 0, fmt.Errorf("read log volume usage on node %s: %w", pod.Spec.NodeName, err)
	}
	var summary struct {
		Pods []struct {
			PodRef struct {
				UID string `json:"uid"`
			} `json:"podRef"`
			Volumes []struct {
				PVCRef *struct {
					Name      string `json:"name"`
					Namespace string `json:"namespace"`
				} `json:"pvcRef"`
				Used     *int64 `json:"usedBytes"`
				Capacity *int64 `json:"capacityBytes"`
			} `json:"volume"`
		} `json:"pods"`
	}
	if err := json.Unmarshal(data, &summary); err != nil {
		return 0, err
	}
	for _, entry := range summary.Pods {
		if entry.PodRef.UID != string(pod.UID) {
			continue
		}
		for _, volume := range entry.Volumes {
			if volume.PVCRef == nil || volume.PVCRef.Name != claim || volume.PVCRef.Namespace != pod.Namespace {
				continue
			}
			if volume.Used == nil || volume.Capacity == nil || *volume.Capacity <= 0 || *volume.Used < 0 || *volume.Used > *volume.Capacity {
				return 0, fmt.Errorf("log claim %s has no valid filesystem measurement", claim)
			}
			// Directory provisioners may report the entire backing filesystem. Growing
			// a claim cannot relieve unrelated usage on a filesystem larger than its allocation.
			if *volume.Capacity > allocatedBytes {
				return 0, fmt.Errorf("log claim %s reports filesystem capacity %d above allocated capacity %d; volume-scoped statistics are required", claim, *volume.Capacity, allocatedBytes)
			}
			return *volume.Used, nil
		}
	}
	return 0, fmt.Errorf("kubelet has not reported usage for log claim %s", claim)
}
