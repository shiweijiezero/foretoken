// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Manages recoverable collection-volume aliases without changing borrowed storage.

package modelcache

import (
	"context"
	"encoding/json"
	"fmt"
	"path"
	"reflect"
	"slices"
	"strings"

	"github.com/shiweijiezero/foretoken/control-plane/internal/runtimeconfig"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type modelCacheCollectionStorageState uint8

const (
	collectionStoragePending modelCacheCollectionStorageState = iota
	collectionStorageReady
	collectionStorageManaged
)

// collectionMetadata identifies collection resources by source UID and platform collector.
// Cluster-scoped PVs cannot use namespaced owners and are retired explicitly by this controller.
func (reconciler *Reconciler) collectionMetadata(record modelCacheCollectionRecord, name, namespace string) metav1.ObjectMeta {
	return metav1.ObjectMeta{
		Name: name, Namespace: namespace,
		Labels: map[string]string{CollectionLabel: string(record.UID)},
		Annotations: map[string]string{
			modelCacheCollectionNamespaceAnnotation: record.Namespace,
			modelCacheCollectionNameAnnotation:      record.Name,
			modelCacheCollectionOwnerAnnotation:     reconciler.Namespace + "/" + reconciler.ServiceAccount,
		},
	}
}

// validateCollectionResource restricts adoption and cleanup to this collector's resources for one source.
func (reconciler *Reconciler) validateCollectionResource(object client.Object, record client.ObjectKey, uid types.UID) error {
	annotations := object.GetAnnotations()
	if object.GetLabels()[CollectionLabel] != string(uid) ||
		annotations[modelCacheCollectionNamespaceAnnotation] != record.Namespace ||
		annotations[modelCacheCollectionNameAnnotation] != record.Name ||
		annotations[modelCacheCollectionOwnerAnnotation] != reconciler.Namespace+"/"+reconciler.ServiceAccount {
		return fmt.Errorf("cache collection resource %q belongs to another source or collector", object.GetName())
	}
	return nil
}

// collectionSourceVolume returns the source's proven PV, or nil when no backing volume remains.
// PVC UID and claimRef resolve its identity without persisting a derived volume name.
func (reconciler *Reconciler) collectionSourceVolume(ctx context.Context, record modelCacheCollectionRecord, reference runtimeconfig.ModelCacheReference) (*corev1.PersistentVolume, error) {
	claim := new(corev1.PersistentVolumeClaim)
	var volume *corev1.PersistentVolume
	if err := reconciler.APIReader.Get(ctx, client.ObjectKey{Namespace: record.Namespace, Name: reference.ClaimName}, claim); err == nil {
		if claim.UID != reference.ClaimUID {
			return nil, fmt.Errorf("model cache claim %s/%s no longer matches its source reference", record.Namespace, reference.ClaimName)
		}
		owner, err := runtimeconfig.ParseStorageIdentity(claim.Annotations[runtimeconfig.DirectoryOwnerAnnotation])
		if err != nil {
			return nil, err
		}
		identity := runtimeconfig.DefaultStorageIdentity()
		if reference.DirectoryOwner != nil {
			identity = *reference.DirectoryOwner
		}
		if owner != nil && *owner != identity {
			return nil, fmt.Errorf("model cache claim %s/%s changed its directory owner", record.Namespace, reference.ClaimName)
		}
		if claim.Spec.VolumeName == "" {
			return nil, fmt.Errorf("model cache claim %s/%s has no bound volume", record.Namespace, reference.ClaimName)
		}
		volume = new(corev1.PersistentVolume)
		if err := reconciler.APIReader.Get(ctx, client.ObjectKey{Name: claim.Spec.VolumeName}, volume); apierrors.IsNotFound(err) {
			return nil, nil
		} else if err != nil {
			return nil, fmt.Errorf("read model cache source volume: %w", err)
		}
	} else if apierrors.IsNotFound(err) {
		var volumes corev1.PersistentVolumeList
		if err := reconciler.APIReader.List(ctx, &volumes); err != nil {
			return nil, fmt.Errorf("find model cache source volume: %w", err)
		}
		for index := range volumes.Items {
			candidate := &volumes.Items[index]
			binding := candidate.Spec.ClaimRef
			if binding == nil || binding.Namespace != record.Namespace || binding.Name != reference.ClaimName || binding.UID != reference.ClaimUID {
				continue
			}
			if volume != nil {
				return nil, fmt.Errorf("multiple volumes reference model cache claim %s/%s UID %s", record.Namespace, reference.ClaimName, reference.ClaimUID)
			}
			volume = candidate
		}
		if volume == nil {
			return nil, nil
		}
	} else {
		return nil, fmt.Errorf("read model cache source claim: %w", err)
	}
	binding := volume.Spec.ClaimRef
	if binding == nil || binding.Namespace != record.Namespace || binding.Name != reference.ClaimName || binding.UID != reference.ClaimUID {
		return nil, fmt.Errorf("model cache volume %q is not bound to the recorded claim UID", volume.Name)
	}
	return volume, nil
}

// collectionAliasSpec preserves mount and scheduling settings for supported filesystem sources.
// NFS CSI paths follow NodePublishVolume's volumeAttributes, never volumeHandle.
func collectionAliasSpec(volume *corev1.PersistentVolume) (corev1.PersistentVolumeSpec, error) {
	spec := *volume.Spec.DeepCopy()
	if spec.VolumeMode != nil && *spec.VolumeMode != corev1.PersistentVolumeFilesystem {
		return spec, fmt.Errorf("model cache volume %q is not a filesystem", volume.Name)
	}
	switch {
	case spec.NFS != nil:
		if spec.NFS.ReadOnly {
			return spec, fmt.Errorf("model cache volume %q is read-only", volume.Name)
		}
	case spec.HostPath != nil:
		if !slices.Contains(spec.AccessModes, corev1.ReadWriteMany) && (spec.NodeAffinity == nil || spec.NodeAffinity.Required == nil) {
			return spec, fmt.Errorf("model cache hostPath volume %q requires node affinity for single-node access", volume.Name)
		}
		directoryType := corev1.HostPathDirectory
		spec.HostPath.Type = &directoryType
	case spec.CSI != nil && spec.CSI.Driver == "nfs.csi.k8s.io":
		if spec.CSI.ReadOnly {
			return spec, fmt.Errorf("model cache volume %q is read-only", volume.Name)
		}
		attributes := make(map[string]string, len(spec.CSI.VolumeAttributes))
		for key, value := range spec.CSI.VolumeAttributes {
			key = strings.ToLower(key)
			switch key {
			case "server", "share", "subdir", "mountoptions",
				"csi.storage.k8s.io/pvc/namespace", "csi.storage.k8s.io/pvc/name", "csi.storage.k8s.io/pv/name":
			default:
				continue
			}
			// Conflicting case variants make the upstream mount depend on map iteration order.
			if previous, exists := attributes[key]; exists && previous != value {
				return spec, fmt.Errorf("model cache volume %q has conflicting NFS CSI attribute %q", volume.Name, key)
			}
			attributes[key] = value
		}
		server, share, subdirectory := attributes["server"], attributes["share"], attributes["subdir"]
		if server == "" || !path.IsAbs(share) {
			return spec, fmt.Errorf("model cache volume %q requires an NFS CSI server and absolute share", volume.Name)
		}
		for placeholder, key := range map[string]string{
			"${pvc.metadata.namespace}": "csi.storage.k8s.io/pvc/namespace",
			"${pvc.metadata.name}":      "csi.storage.k8s.io/pvc/name",
			"${pv.metadata.name}":       "csi.storage.k8s.io/pv/name",
		} {
			if value, present := attributes[key]; present {
				subdirectory = strings.ReplaceAll(subdirectory, placeholder, value)
			}
		}
		if subdirectory != "" {
			share = strings.TrimRight(share, "/") + "/" + subdirectory
		}
		if server == "" || !path.IsAbs(share) || strings.Contains(share, "\\") || slices.Contains(strings.Split(share, "/"), "..") {
			return spec, fmt.Errorf("model cache volume %q requires an NFS CSI server and absolute share/subdir path", volume.Name)
		}
		if options := attributes["mountoptions"]; options != "" {
			spec.MountOptions = append(spec.MountOptions, options)
		}
		spec.PersistentVolumeSource = corev1.PersistentVolumeSource{NFS: &corev1.NFSVolumeSource{Server: server, Path: share}}
	default:
		return spec, fmt.Errorf("model cache volume %q requires native NFS, hostPath or nfs.csi.k8s.io storage", volume.Name)
	}
	for _, entry := range spec.MountOptions {
		for _, option := range strings.Split(entry, ",") {
			if strings.TrimSpace(option) == "ro" {
				return spec, fmt.Errorf("model cache volume %q has read-only mount options", volume.Name)
			}
		}
	}
	capacity := spec.Capacity[corev1.ResourceStorage]
	if capacity.Sign() <= 0 || len(spec.AccessModes) == 0 {
		return spec, fmt.Errorf("model cache volume %q has no binding capacity or access modes", volume.Name)
	}
	filesystem := corev1.PersistentVolumeFilesystem
	spec.VolumeMode = &filesystem
	spec.StorageClassName = ""
	spec.VolumeAttributesClassName = nil
	spec.PersistentVolumeReclaimPolicy = corev1.PersistentVolumeReclaimRetain
	spec.ClaimRef = nil
	return spec, nil
}

// reconcileCollectionStorage prepares supported aliases or defers to the cache's storage lifecycle.
// The platform claim retains the source spec so restarts and source-namespace removal can resume collection.
func (reconciler *Reconciler) reconcileCollectionStorage(ctx context.Context, plan *modelCacheCollectionPlan) (modelCacheCollectionStorageState, error) {
	name := modelCacheCollectionStoragePrefix + string(plan.Record.UID)
	key := client.ObjectKey{Namespace: reconciler.Namespace, Name: name}
	recordKey := client.ObjectKey{Namespace: plan.Record.Namespace, Name: plan.Record.Name}
	claim := new(corev1.PersistentVolumeClaim)
	if err := reconciler.APIReader.Get(ctx, key, claim); apierrors.IsNotFound(err) {
		source, err := reconciler.collectionSourceVolume(ctx, plan.Record, plan.Cache)
		if err != nil {
			return collectionStoragePending, err
		}
		if source == nil {
			if plan.Cache.Directory == "" {
				return collectionStorageManaged, nil
			}
			return collectionStoragePending, fmt.Errorf("no volume remains bound to model cache claim %s/%s UID %s", plan.Record.Namespace, plan.Cache.ClaimName, plan.Cache.ClaimUID)
		}
		// Other provisioned storage keeps its driver's existing retention lifecycle.
		supported := source.Spec.NFS != nil || source.Spec.HostPath != nil || source.Spec.CSI != nil && source.Spec.CSI.Driver == "nfs.csi.k8s.io"
		if plan.Cache.Directory == "" && !supported {
			return collectionStorageManaged, nil
		}
		spec, err := collectionAliasSpec(source)
		if err != nil {
			return collectionStoragePending, err
		}
		// Persist only source identity and spec; annotations and lifecycle ownership remain with the source.
		snapshot := corev1.PersistentVolume{ObjectMeta: metav1.ObjectMeta{Name: source.Name, UID: source.UID}, Spec: *source.Spec.DeepCopy()}
		encoded, err := json.Marshal(snapshot)
		if err != nil {
			return collectionStoragePending, err
		}
		class := ""
		claim = &corev1.PersistentVolumeClaim{
			ObjectMeta: reconciler.collectionMetadata(plan.Record, name, reconciler.Namespace),
			Spec: corev1.PersistentVolumeClaimSpec{
				AccessModes: slices.Clone(spec.AccessModes), VolumeMode: spec.VolumeMode,
				StorageClassName: &class, VolumeName: name,
				Resources: corev1.VolumeResourceRequirements{Requests: spec.Capacity.DeepCopy()},
			},
		}
		claim.Annotations[modelCacheCollectionSourceVolumeAnnotation] = string(encoded)
		if err := reconciler.Create(ctx, claim); err != nil {
			return collectionStoragePending, fmt.Errorf("create model cache collection claim: %w", err)
		}
	} else if err != nil {
		return collectionStoragePending, err
	}
	if err := reconciler.validateCollectionResource(claim, recordKey, plan.Record.UID); err != nil {
		return collectionStoragePending, err
	}
	if !claim.DeletionTimestamp.IsZero() {
		return collectionStoragePending, fmt.Errorf("model cache collection claim %q is being deleted", name)
	}
	var source corev1.PersistentVolume
	encoded := claim.Annotations[modelCacheCollectionSourceVolumeAnnotation]
	if err := json.Unmarshal([]byte(encoded), &source); err != nil {
		return collectionStoragePending, fmt.Errorf("decode model cache collection volume origin: %w", err)
	}
	binding := source.Spec.ClaimRef
	if source.Name == "" || source.UID == "" || binding == nil || binding.Namespace != plan.Record.Namespace || binding.Name != plan.Cache.ClaimName || binding.UID != plan.Cache.ClaimUID {
		return collectionStoragePending, fmt.Errorf("model cache collection claim %q has a different volume origin", name)
	}
	spec, err := collectionAliasSpec(&source)
	if err != nil {
		return collectionStoragePending, err
	}
	requested, capacity := claim.Spec.Resources.Requests[corev1.ResourceStorage], spec.Capacity[corev1.ResourceStorage]
	if claim.Spec.VolumeName != name || claim.Spec.StorageClassName == nil || *claim.Spec.StorageClassName != "" || !slices.Equal(claim.Spec.AccessModes, spec.AccessModes) || !reflect.DeepEqual(claim.Spec.VolumeMode, spec.VolumeMode) || requested.Cmp(capacity) != 0 {
		return collectionStoragePending, fmt.Errorf("model cache collection claim %q changed its binding", name)
	}
	spec.ClaimRef = &corev1.ObjectReference{APIVersion: "v1", Kind: "PersistentVolumeClaim", Namespace: reconciler.Namespace, Name: claim.Name, UID: claim.UID}
	volume := new(corev1.PersistentVolume)
	if err := reconciler.APIReader.Get(ctx, client.ObjectKey{Name: name}, volume); apierrors.IsNotFound(err) {
		volume = &corev1.PersistentVolume{ObjectMeta: reconciler.collectionMetadata(plan.Record, name, ""), Spec: spec}
		volume.Annotations[modelCacheCollectionClaimUIDAnnotation] = string(claim.UID)
		volume.Annotations[modelCacheCollectionSourceVolumeAnnotation] = encoded
		if err := reconciler.Create(ctx, volume); err != nil {
			return collectionStoragePending, fmt.Errorf("create model cache collection volume: %w", err)
		}
	} else if err != nil {
		return collectionStoragePending, err
	}
	if err := reconciler.validateCollectionResource(volume, recordKey, plan.Record.UID); err != nil {
		return collectionStoragePending, err
	}
	actual := volume.Spec.ClaimRef
	actualCapacity := volume.Spec.Capacity[corev1.ResourceStorage]
	if !volume.DeletionTimestamp.IsZero() || actual == nil || actual.Namespace != reconciler.Namespace || actual.Name != claim.Name || actual.UID != claim.UID ||
		volume.Annotations[modelCacheCollectionClaimUIDAnnotation] != string(claim.UID) || volume.Annotations[modelCacheCollectionSourceVolumeAnnotation] != encoded ||
		!reflect.DeepEqual(volume.Spec.PersistentVolumeSource, spec.PersistentVolumeSource) || !slices.Equal(volume.Spec.MountOptions, spec.MountOptions) ||
		!reflect.DeepEqual(volume.Spec.NodeAffinity, spec.NodeAffinity) || !slices.Equal(volume.Spec.AccessModes, spec.AccessModes) ||
		volume.Spec.StorageClassName != "" || volume.Spec.PersistentVolumeReclaimPolicy != corev1.PersistentVolumeReclaimRetain ||
		!reflect.DeepEqual(volume.Spec.VolumeMode, spec.VolumeMode) || actualCapacity.Cmp(capacity) != 0 {
		return collectionStoragePending, fmt.Errorf("model cache collection volume %q changed its origin or claim UID", name)
	}
	plan.Volume = modelCacheCollectionVolume{Name: source.Name, UID: source.UID, ClaimRef: *source.Spec.ClaimRef.DeepCopy(), Source: *source.Spec.PersistentVolumeSource.DeepCopy()}
	plan.CollectionVolume = modelCacheCollectionAlias{Name: volume.Name, UID: volume.UID, ClaimName: claim.Name, ClaimUID: claim.UID}
	if claim.Status.Phase == corev1.ClaimBound {
		return collectionStorageReady, nil
	}
	return collectionStoragePending, nil
}
