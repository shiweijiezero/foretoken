// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Prepares platform-authorized filesystem bindings borrowed by namespace workloads.
package deployments

import (
	"context"
	"encoding/json"
	"fmt"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// prepareDirectories materializes authorized filesystem aliases once per namespace.
// Instances borrow these bindings; deleting an instance never removes shared storage.
func (r *Reconciler) prepareDirectories(ctx context.Context, namespace string, selection *controllerSelection) (map[string]string, error) {
	bindings := make(map[string]string)
	var namespaceUID types.UID
	for name, directory := range selection.Template.Directories {
		if directory.ExistingClaim {
			claim := new(corev1.PersistentVolumeClaim)
			if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: namespace, Name: name}, claim); err != nil {
				return nil, fmt.Errorf("read provided directory claim: %w", err)
			}
			if !claim.DeletionTimestamp.IsZero() {
				return nil, fmt.Errorf("directory claim %s/%s is deleting", namespace, name)
			}
			bindings[directory.Directory] = name
			continue
		}
		if namespaceUID == "" {
			value := new(corev1.Namespace)
			if err := r.APIReader.Get(ctx, client.ObjectKey{Name: namespace}, value); err != nil {
				return nil, err
			}
			namespaceUID = value.UID
		}
		size, err := resource.ParseQuantity(directory.Capacity)
		if err != nil || size.Sign() <= 0 {
			return nil, fmt.Errorf("directory %q requires a positive capacity", directory.Directory)
		}
		volumeName := "foretoken." + string(namespaceUID) + "." + name
		annotations := map[string]string{"inference.foretoken.io/runtime-cache-directory": directory.Directory}
		if directory.DirectoryOwner != "" {
			if _, err := runtimeconfig.ParseStorageIdentity(directory.DirectoryOwner); err != nil {
				return nil, err
			}
			annotations[runtimeconfig.DirectoryOwnerAnnotation] = directory.DirectoryOwner
		}
		volume := &corev1.PersistentVolume{
			TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "PersistentVolume"},
			ObjectMeta: metav1.ObjectMeta{Name: volumeName, Annotations: annotations, Labels: map[string]string{platformUIDLabel: string(selection.PlatformUID)}},
			Spec: corev1.PersistentVolumeSpec{
				Capacity:                      corev1.ResourceList{corev1.ResourceStorage: size},
				AccessModes:                   []corev1.PersistentVolumeAccessMode{corev1.ReadWriteMany},
				PersistentVolumeReclaimPolicy: corev1.PersistentVolumeReclaimRetain,
				ClaimRef:                      &corev1.ObjectReference{Namespace: namespace, Name: name},
				MountOptions:                  directory.MountOptions,
				NodeAffinity:                  directory.NodeAffinity.DeepCopy(),
			},
		}
		switch {
		case directory.Server != "" && directory.Path != "":
			volume.Spec.NFS = &corev1.NFSVolumeSource{Server: directory.Server, Path: directory.Path}
		case directory.HostPath != "":
			pathType := corev1.HostPathDirectory
			volume.Spec.HostPath = &corev1.HostPathVolumeSource{Path: directory.HostPath, Type: &pathType}
			if len(directory.Hostnames) > 0 {
				volume.Spec.NodeAffinity = &corev1.VolumeNodeAffinity{Required: &corev1.NodeSelector{NodeSelectorTerms: []corev1.NodeSelectorTerm{{MatchExpressions: []corev1.NodeSelectorRequirement{{Key: corev1.LabelHostname, Operator: corev1.NodeSelectorOpIn, Values: directory.Hostnames}}}}}}
			}
		default:
			return nil, fmt.Errorf("directory %q has no prepared NFS or host binding", directory.Directory)
		}
		emptyClass := ""
		claim := &corev1.PersistentVolumeClaim{
			TypeMeta:   metav1.TypeMeta{APIVersion: "v1", Kind: "PersistentVolumeClaim"},
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: namespace, Annotations: annotations, Labels: map[string]string{platformUIDLabel: string(selection.PlatformUID)}},
			Spec: corev1.PersistentVolumeClaimSpec{
				AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteMany},
				StorageClassName: &emptyClass, VolumeName: volumeName,
				Resources: corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: size}},
			},
		}
		// A prebound claim can wait for its PV. Create it first so both initial
		// binding and recovery select an actual PVC UID, never just a reusable name.
		if err := r.applyDirectory(ctx, selection.PlatformUID, claim); err != nil {
			return nil, err
		}
		volume.Spec.ClaimRef.UID = claim.UID
		if err := r.applyDirectory(ctx, selection.PlatformUID, volume); err != nil {
			return nil, err
		}
		bindings[directory.Directory] = name
	}
	return bindings, nil
}

// publishRuntimeCacheTopology observes only this deployment's selected cache claim.
// PVC metadata carries the bound-volume constraint without transferring PV access or PVC ownership.
func (r *Reconciler) publishRuntimeCacheTopology(ctx context.Context, deployment *api.InferenceDeployment, container *corev1.Container, bindings runtimeconfig.RuntimeCacheDirectoryBindings) error {
	profile := runtimeconfig.RuntimeCacheProfile{
		ClaimName: controllerArgument(container, "cache-claim"), MountPath: controllerArgument(container, "cache-mount-path"),
		DirectoryBindings: bindings,
	}
	reader := &scopedReader{Reader: r.APIReader, metadata: r.Client, scope: Scope{Namespace: deployment.Namespace, UID: string(deployment.UID)}}
	binding, ready, err := profile.Resolve(ctx, reader, deployment.Namespace)
	if err != nil {
		return client.IgnoreNotFound(err)
	}
	if !ready || binding == nil {
		return nil
	}
	claim := new(corev1.PersistentVolumeClaim)
	if err := r.APIReader.Get(ctx, client.ObjectKey{Namespace: deployment.Namespace, Name: binding.ClaimName}, claim); err != nil {
		return client.IgnoreNotFound(err)
	}
	encoded := ""
	if claim.Status.Phase == corev1.ClaimBound && claim.Spec.VolumeName != "" {
		volume := new(corev1.PersistentVolume)
		if err := r.APIReader.Get(ctx, client.ObjectKey{Name: claim.Spec.VolumeName}, volume); err != nil && !apierrors.IsNotFound(err) {
			return fmt.Errorf("read runtime cache bound volume: %w", err)
		}
		if ref := volume.Spec.ClaimRef; ref != nil && ref.Namespace == claim.Namespace && ref.Name == claim.Name && ref.UID == claim.UID {
			topology := runtimeconfig.RuntimeCacheVolumeTopology{
				ClaimUID: claim.UID, VolumeName: volume.Name, NodeAffinity: volume.Spec.NodeAffinity,
			}
			data, err := json.Marshal(topology)
			if err != nil {
				return fmt.Errorf("encode runtime cache volume topology: %w", err)
			}
			encoded = string(data)
		}
	}
	if claim.Annotations[runtimeconfig.RuntimeCacheVolumeTopologyAnnotation] == encoded {
		return nil
	}
	base := claim.DeepCopy()
	if encoded == "" {
		delete(claim.Annotations, runtimeconfig.RuntimeCacheVolumeTopologyAnnotation)
	} else {
		if claim.Annotations == nil {
			claim.Annotations = make(map[string]string)
		}
		claim.Annotations[runtimeconfig.RuntimeCacheVolumeTopologyAnnotation] = encoded
	}
	patch, err := identityPatch(claim, client.MergeFrom(base))
	if err != nil {
		return err
	}
	if err := r.Patch(ctx, claim, patch); err != nil {
		return fmt.Errorf("publish runtime cache volume topology: %w", err)
	}
	return nil
}

// runtimeCacheDeployments wakes namespace deployments when a cache selection or binding changes.
// Reconciliation resolves each deployment's actual claim before publishing any metadata.
func (r *Reconciler) runtimeCacheDeployments(ctx context.Context, object client.Object) []ctrl.Request {
	namespace := object.GetNamespace()
	if volume, ok := object.(*corev1.PersistentVolume); ok && volume.Spec.ClaimRef != nil {
		namespace = volume.Spec.ClaimRef.Namespace
	}
	if namespace == "" {
		return nil
	}
	var deployments api.InferenceDeploymentList
	if err := r.List(ctx, &deployments, client.InNamespace(namespace)); err != nil {
		ctrl.LoggerFrom(ctx).Error(err, "list deployments for runtime cache observation")
		return nil
	}
	requests := make([]ctrl.Request, 0, len(deployments.Items))
	for _, deployment := range deployments.Items {
		requests = append(requests, ctrl.Request{NamespacedName: client.ObjectKeyFromObject(&deployment)})
	}
	return requests
}

// applyDirectory uses the platform identity rather than whichever instance first requested storage.
func (r *Reconciler) applyDirectory(ctx context.Context, platformUID types.UID, desired client.Object) error {
	current := desired.DeepCopyObject().(client.Object)
	if err := r.APIReader.Get(ctx, client.ObjectKeyFromObject(desired), current); apierrors.IsNotFound(err) {
		return r.Create(ctx, desired, client.FieldOwner("foretoken-directory-bindings"))
	} else if err != nil {
		return err
	} else {
		if current.GetLabels()[platformUIDLabel] != string(platformUID) || !current.GetDeletionTimestamp().IsZero() {
			return fmt.Errorf("directory binding %s/%s is not available to this platform", current.GetNamespace(), current.GetName())
		}
		desired.SetUID(current.GetUID())
		desired.SetResourceVersion(current.GetResourceVersion())
		if volume, ok := desired.(*corev1.PersistentVolume); ok {
			bound := current.(*corev1.PersistentVolume).Spec.ClaimRef
			if bound != nil {
				if bound.Namespace != volume.Spec.ClaimRef.Namespace || bound.Name != volume.Spec.ClaimRef.Name {
					return fmt.Errorf("directory volume %s is bound to another claim", volume.Name)
				}
				if bound.UID == volume.Spec.ClaimRef.UID {
					volume.Spec.ClaimRef = bound.DeepCopy()
				} else if bound.UID != "" {
					previous := current.(*corev1.PersistentVolume)
					if previous.Spec.PersistentVolumeReclaimPolicy != corev1.PersistentVolumeReclaimRetain {
						return fmt.Errorf("directory volume %s no longer retains its data", volume.Name)
					}
					if previous.Status.Phase != corev1.VolumeReleased {
						return fmt.Errorf("waiting for directory volume %s to release its previous claim", volume.Name)
					}
				}
			}
		}
	}
	return r.Patch(ctx, desired, client.Apply, client.FieldOwner("foretoken-directory-bindings"), client.ForceOwnership)
}
