// SPDX-License-Identifier: Apache-2.0
// SPDX-FileCopyrightText: Copyright contributors to the Foretoken project

// Projects platform-selected application files into workload startup.
package runtimeconfig

import (
	"maps"
	"net/url"
	"path"
	"strings"

	corev1 "k8s.io/api/core/v1"
)

// ApplicationFiles contains the platform's file origin, download tools and mount layout.
// Controllers use it to prepare immutable application versions before starting a workload.
type ApplicationFiles struct {
	Origin    string `json:"origin"`
	Image     string `json:"image"`
	Script    string `json:"script"`
	MountPath string `json:"mountPath"`
}

// Ref resolves a service's selected component version at the trusted platform origin.
// An absent revision produces no source override.
func (files ApplicationFiles) Ref(component, revision string) string {
	if revision == "" {
		return ""
	}
	return strings.TrimRight(files.Origin, "/") + "/" + url.PathEscape(component) + "/" + url.PathEscape(revision)
}

// Directory returns the completed Pod-local application directory consumed at startup.
func (files ApplicationFiles) Directory() string {
	return path.Join(files.MountPath, "current")
}

// Configure prepares the selected files in a Pod-local volume and starts its executable.
// The Pod owns the application volume; persistent model storage remains independent.
func (files ApplicationFiles) Configure(template *corev1.PodTemplateSpec, container *corev1.Container, reference, executable string) {
	if reference == "" {
		return
	}
	template.Labels = maps.Clone(template.Labels)
	if template.Labels == nil {
		template.Labels = make(map[string]string)
	}
	template.Labels["foretoken.io/application-files"] = "consumer"
	template.Annotations = maps.Clone(template.Annotations)
	if template.Annotations == nil {
		template.Annotations = make(map[string]string)
	}
	template.Annotations["inference.foretoken.io/application-url"] = reference
	pod := &template.Spec
	// Download under an explicit workload identity without changing persistent-volume ownership.
	user, group := int64(65532), int64(65532)
	if context := pod.SecurityContext; context != nil {
		if context.RunAsUser != nil {
			user = *context.RunAsUser
		}
		if context.RunAsGroup != nil {
			group = *context.RunAsGroup
		}
	}
	noEscalation, readOnly := false, true
	pod.Volumes = append(pod.Volumes, corev1.Volume{
		Name: "application", VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}},
	})
	pod.InitContainers = append(pod.InitContainers, corev1.Container{
		Name: "application-files", Image: files.Image, ImagePullPolicy: corev1.PullIfNotPresent,
		Command:      []string{"python", "-c", files.Script, reference, files.MountPath},
		VolumeMounts: []corev1.VolumeMount{{Name: "application", MountPath: files.MountPath}},
		SecurityContext: &corev1.SecurityContext{
			RunAsUser: &user, RunAsGroup: &group,
			AllowPrivilegeEscalation: &noEscalation, ReadOnlyRootFilesystem: &readOnly,
			Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	})
	container.VolumeMounts = append(container.VolumeMounts, corev1.VolumeMount{
		Name: "application", MountPath: files.MountPath, ReadOnly: true,
	})
	command := path.Join(files.Directory(), "bin", executable)
	if len(container.Command) == 0 {
		container.Command = []string{command}
	} else {
		container.Command[0] = command
	}
}
