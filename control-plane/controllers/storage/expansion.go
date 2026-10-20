// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Shares bounded PVC growth and resize-state interpretation between storage owners.

package storage

import (
	"time"

	corev1 "k8s.io/api/core/v1"
)

const (
	// PVCPollInterval schedules rechecks while PVC binding or expansion is pending.
	PVCPollInterval = 5 * time.Second
	// PVCExpansionFreeSpaceDivisor defines the free-space fraction that prompts proactive growth.
	PVCExpansionFreeSpaceDivisor int64 = 5
)

// NextPVCSize doubles a storage owner's requested capacity without exceeding its configured maximum.
func NextPVCSize(current, maximum int64) int64 {
	if current <= maximum/2 {
		return current * 2
	}
	return maximum
}

// PVCCapacityAtLeastRequest reports whether Kubernetes has supplied the claim's requested capacity.
func PVCCapacityAtLeastRequest(pvc *corev1.PersistentVolumeClaim) bool {
	capacity := pvc.Status.Capacity[corev1.ResourceStorage]
	requested := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
	return capacity.Cmp(requested) >= 0
}

// PVCResizePending reports whether the claim is waiting for controller or filesystem expansion.
func PVCResizePending(pvc *corev1.PersistentVolumeClaim) bool {
	for _, condition := range pvc.Status.Conditions {
		if condition.Status == corev1.ConditionTrue && (condition.Type == corev1.PersistentVolumeClaimResizing || condition.Type == corev1.PersistentVolumeClaimFileSystemResizePending) {
			return true
		}
	}
	status := pvc.Status.AllocatedResourceStatuses[corev1.ResourceStorage]
	return status == corev1.PersistentVolumeClaimControllerResizeInProgress ||
		status == corev1.PersistentVolumeClaimNodeResizePending ||
		status == corev1.PersistentVolumeClaimNodeResizeInProgress
}

// PVCResizeFailure returns Kubernetes' diagnostic when volume or filesystem expansion fails.
func PVCResizeFailure(pvc *corev1.PersistentVolumeClaim) (string, bool) {
	for _, condition := range pvc.Status.Conditions {
		if condition.Status != corev1.ConditionTrue || condition.Type != corev1.PersistentVolumeClaimControllerResizeError && condition.Type != corev1.PersistentVolumeClaimNodeResizeError {
			continue
		}
		if condition.Message != "" {
			return condition.Message, true
		}
		return "Kubernetes reported a PVC resize error", true
	}
	status := pvc.Status.AllocatedResourceStatuses[corev1.ResourceStorage]
	if status == corev1.PersistentVolumeClaimControllerResizeInfeasible || status == corev1.PersistentVolumeClaimNodeResizeInfeasible {
		return "Kubernetes reported that the PVC resize cannot complete", true
	}
	return "", false
}
