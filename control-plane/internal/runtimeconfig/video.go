// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Resolves storage identity shared by video submission and worker workloads.

package runtimeconfig

import (
	"context"
	"fmt"

	inferencev1alpha1 "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const VideoTaskOutputMount = "/var/lib/foretoken/video-tasks"

// VideoOutputIdentity resolves the borrowed output claim's user before persisting a task plan.
func VideoOutputIdentity(ctx context.Context, reader client.Reader, namespace, claimName string) (inferencev1alpha1.StorageIdentity, error) {
	claim := new(corev1.PersistentVolumeClaim)
	if err := reader.Get(ctx, client.ObjectKey{Namespace: namespace, Name: claimName}, claim); err != nil {
		return inferencev1alpha1.StorageIdentity{}, fmt.Errorf("read video output claim %q: %w", claimName, err)
	}
	identity, err := ParseStorageIdentity(claim.Annotations[DirectoryOwnerAnnotation])
	if err != nil {
		return inferencev1alpha1.StorageIdentity{}, fmt.Errorf("resolve video output claim %q identity: %w", claimName, err)
	}
	if identity == nil {
		return inferencev1alpha1.StorageIdentity{UID: 65532, GID: 65532}, nil
	}
	return *identity, nil
}
