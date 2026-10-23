// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Expands platform log storage using kubelet filesystem observations.

package logstorage

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/shiweijiezero/foretoken/control-plane/controllers/storage"
	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	statsv1alpha1 "k8s.io/kubelet/pkg/apis/stats/v1alpha1"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Reconciler grows claims mounted by the selected platform Loki StatefulSet.
// The maintenance command invokes it once; Helm owns the workload and initial claim template.
type Reconciler struct {
	client.Client
	Kubernetes *kubernetes.Clientset
	MaxSize    resource.Quantity
}

// Reconcile reads the mounted log filesystem and requests bounded growth through Kubernetes.
// Missing measurements and unsupported storage surface as reconciliation errors, not zero usage.
func (r *Reconciler) Reconcile(ctx context.Context, request ctrl.Request) (ctrl.Result, error) {
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
	return ctrl.Result{}, nil
}

// expandClaim waits for pending resizes and uses live PVC requests to avoid repeating growth
// against a pre-expansion kubelet measurement. It never shrinks or replaces retained storage.
func (r *Reconciler) expandClaim(ctx context.Context, pod *corev1.Pod, name string) error {
	pvc := new(corev1.PersistentVolumeClaim)
	if err := r.Get(ctx, client.ObjectKey{Namespace: pod.Namespace, Name: name}, pvc); err != nil {
		return err
	}
	if !pvc.DeletionTimestamp.IsZero() || pvc.Status.Phase != corev1.ClaimBound {
		return nil
	}
	if message, failed := storage.PVCResizeFailure(pvc); failed {
		return fmt.Errorf("log claim %s: %s", name, message)
	}
	if !storage.PVCCapacityAtLeastRequest(pvc) || storage.PVCResizePending(pvc) {
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
	if used < requested-requested/storage.PVCExpansionFreeSpaceDivisor {
		return nil
	}
	target := storage.NextPVCSize(requested, r.MaxSize.Value())
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
func (r *Reconciler) filesystem(ctx context.Context, pod *corev1.Pod, claim string, allocatedBytes int64) (int64, error) {
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()
	data, err := r.Kubernetes.CoreV1().RESTClient().Get().Resource("nodes").Name(pod.Spec.NodeName).
		SubResource("proxy").Suffix("stats", "summary").DoRaw(ctx)
	if err != nil {
		return 0, fmt.Errorf("read log volume usage on node %s: %w", pod.Spec.NodeName, err)
	}
	var summary statsv1alpha1.Summary
	if err := json.Unmarshal(data, &summary); err != nil {
		return 0, err
	}
	for _, entry := range summary.Pods {
		if entry.PodRef.UID != string(pod.UID) {
			continue
		}
		for _, volume := range entry.VolumeStats {
			if volume.PVCRef == nil || volume.PVCRef.Name != claim || volume.PVCRef.Namespace != pod.Namespace {
				continue
			}
			if volume.UsedBytes == nil || volume.CapacityBytes == nil || *volume.CapacityBytes == 0 || *volume.UsedBytes > *volume.CapacityBytes {
				return 0, fmt.Errorf("log claim %s has no valid filesystem measurement", claim)
			}
			// Directory provisioners may report the entire backing filesystem. Growing
			// a claim cannot relieve unrelated usage on a filesystem larger than its allocation.
			if *volume.CapacityBytes > uint64(allocatedBytes) {
				return 0, fmt.Errorf("log claim %s reports filesystem capacity %d above allocated capacity %d; volume-scoped statistics are required", claim, *volume.CapacityBytes, allocatedBytes)
			}
			return int64(*volume.UsedBytes), nil
		}
	}
	return 0, fmt.Errorf("kubelet has not reported usage for log claim %s", claim)
}
