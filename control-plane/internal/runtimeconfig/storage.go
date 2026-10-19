// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

package runtimeconfig

import (
	"fmt"
	"path"
	"slices"
	"strconv"
	"strings"

	api "github.com/shiweijiezero/foretoken/control-plane/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
)

const DirectoryOwnerAnnotation = "inference.foretoken.io/directory-owner"

// CacheObservationPort selects the cache observer endpoint next to a runtime port.
func CacheObservationPort(runtimePort int32) int32 {
	if runtimePort < 65535 {
		return runtimePort + 1
	}
	return runtimePort - 1
}

// DefaultStorageIdentity is the platform identity for new runtime-owned files.
func DefaultStorageIdentity() api.StorageIdentity {
	return api.StorageIdentity{UID: 1000, GID: 1000}
}

// ParseStorageIdentity reads a discovered directory owner without inventing one for absent metadata.
func ParseStorageIdentity(value string) (*api.StorageIdentity, error) {
	if value == "" {
		return nil, nil
	}
	uidText, gidText, found := strings.Cut(value, ":")
	uid, uidErr := strconv.ParseUint(uidText, 10, 32)
	gid, gidErr := strconv.ParseUint(gidText, 10, 32)
	if !found || uidErr != nil || gidErr != nil || uid == 0 || uid == 1<<32-1 || gid == 1<<32-1 {
		return nil, fmt.Errorf("invalid storage identity %q", value)
	}
	return &api.StorageIdentity{UID: int64(uid), GID: int64(gid)}, nil
}

// VolumeAccess associates one mounted volume with its resolved filesystem identity.
// PrepareRoot authorizes permission changes to the mount root, never its existing contents.
// Directories lists controller-derived relative paths to create and share under the volume identity.
type VolumeAccess struct {
	Name        string
	Identity    api.StorageIdentity
	PrepareRoot bool
	Directories []string
}

// CacheStorageAccess preserves a discovered owner or prepares the cache root for the platform user.
// Existing files retain their ownership and permissions in either case.
func CacheStorageAccess(cache *api.RuntimeCacheBinding) VolumeAccess {
	access := VolumeAccess{Name: "runtime-cache", Identity: DefaultStorageIdentity()}
	if cache != nil {
		access.PrepareRoot = cache.DirectoryOwner == nil
		if cache.DirectoryOwner != nil {
			access.Identity = *cache.DirectoryOwner
		}
	}
	return access
}

// WorkloadCacheAccess confines retained applications to storage outside managed download collection.
// Their local loaders cannot register shared-cache references, so the new shared area is hidden.
func WorkloadCacheAccess(pod *corev1.PodTemplateSpec, cache *api.RuntimeCacheBinding, preparationVersion int32) VolumeAccess {
	access := CacheStorageAccess(cache)
	if cache != nil && preparationVersion < SharedModelCachePreparationVersion {
		access.Directories = []string{"shared"}
		pod.Spec.Volumes = append(pod.Spec.Volumes, corev1.Volume{
			Name: "legacy-shared-cache", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
		})
		pod.Spec.Containers[0].VolumeMounts = append(pod.Spec.Containers[0].VolumeMounts,
			corev1.VolumeMount{Name: "legacy-shared-cache", MountPath: path.Join(cache.MountPath, "shared")})
	}
	return access
}

// ConfigurePodStorage sets the process identity and combines access to independently owned volumes.
// Call after all volumes and init containers are projected. Preparation touches only authorized roots
// and declared directories without walking their contents. Scoped directories retain other users' ownership.
func ConfigurePodStorage(pod *corev1.PodSpec, toolsImage string, identity api.StorageIdentity, volumes ...VolumeAccess) {
	if pod.SecurityContext == nil {
		pod.SecurityContext = &corev1.PodSecurityContext{}
	}
	context := pod.SecurityContext
	nonRoot := true
	context.RunAsNonRoot = &nonRoot
	context.RunAsUser = &identity.UID
	context.RunAsGroup = &identity.GID
	context.FSGroup = nil
	context.FSGroupChangePolicy = nil
	if context.SeccompProfile == nil {
		context.SeccompProfile = &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault}
	}
	for _, access := range volumes {
		if access.Identity.GID != identity.GID && !slices.Contains(context.SupplementalGroups, access.Identity.GID) {
			context.SupplementalGroups = append(context.SupplementalGroups, access.Identity.GID)
		}
	}
	slices.Sort(context.SupplementalGroups)
	mounts := []corev1.VolumeMount{}
	arguments := []string{"python", "-I", "-B", "-c", `import os, sys
for offset in range(1, len(sys.argv), 4):
    mount, relative, uid, gid = sys.argv[offset:offset + 4]
    uid, gid = int(uid), int(gid)
    directory = os.open(mount, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not relative:
            os.fchown(directory, uid, gid)
            os.fchmod(directory, 0o2775)
        else:
            for name in relative.split("/"):
                created = False
                try:
                    os.mkdir(name, 0o770, dir_fd=directory)
                    created = True
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
                os.close(directory)
                directory = child
                info = os.fstat(directory)
                if created or info.st_uid == uid:
                    os.fchown(directory, uid, gid)
                    os.fchmod(directory, 0o2770)
    finally:
        os.close(directory)
`}
	for _, volume := range pod.Volumes {
		owner := identity
		prepare := volume.EmptyDir != nil
		var directories []string
		for _, access := range volumes {
			if access.Name == volume.Name {
				owner = access.Identity
				prepare = prepare || access.PrepareRoot
				directories = access.Directories
				break
			}
		}
		if !prepare && len(directories) == 0 {
			continue
		}
		mount := "/storage/" + volume.Name
		mounts = append(mounts, corev1.VolumeMount{Name: volume.Name, MountPath: mount})
		uid, gid := strconv.FormatInt(owner.UID, 10), strconv.FormatInt(owner.GID, 10)
		if prepare {
			arguments = append(arguments, mount, "", uid, gid)
		}
		for _, directory := range directories {
			arguments = append(arguments, mount, directory, uid, gid)
		}
	}
	if len(mounts) == 0 {
		return
	}
	root, noEscalation, readOnly := int64(0), false, true
	// FSETID preserves setgid after assigning a directory to a different volume group.
	prepare := corev1.Container{
		Name: "storage-permissions", Image: toolsImage, Command: arguments,
		Env: []corev1.EnvVar{{Name: "NVIDIA_VISIBLE_DEVICES", Value: "void"}},
		SecurityContext: &corev1.SecurityContext{
			RunAsNonRoot: &noEscalation, RunAsUser: &root, RunAsGroup: &root,
			AllowPrivilegeEscalation: &noEscalation, ReadOnlyRootFilesystem: &readOnly,
			Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}, Add: []corev1.Capability{"CHOWN", "FOWNER", "FSETID", "DAC_OVERRIDE"}},
		},
		VolumeMounts:             mounts,
		TerminationMessagePolicy: corev1.TerminationMessageFallbackToLogsOnError,
	}
	pod.InitContainers = append([]corev1.Container{prepare}, pod.InitContainers...)
}
